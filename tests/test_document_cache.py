import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from pypdf import PdfWriter

import web_app


def test_index_and_facts_share_parsing_for_concurrent_requests(tmp_path, monkeypatch):
    doc = "a9" * 16
    folder = tmp_path / doc
    folder.mkdir()
    (folder / "document.json").write_text("{}", encoding="utf-8")
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.write(folder / "document.pdf")
    evidence = folder / "evidence.json"

    def save_evidence(text):
        evidence.write_text(json.dumps([{"evidence_id": "E1", "page": 1, "type": "text",
                                        "content": text}], ensure_ascii=False), encoding="utf-8")

    save_evidence("原始资格条件")
    monkeypatch.setattr(web_app, "IMPORTS", tmp_path)
    actual_read = web_app.read_document_inputs
    actual_build = web_app.build_search
    entered = Event()
    release = Event()
    ready = Barrier(3)
    reads, builds = [], []

    def read(*args):
        reads.append(args)
        entered.set()
        assert release.wait(10)
        return actual_read(*args)

    def build(*args, **kwargs):
        builds.append(args)
        return actual_build(*args, **kwargs)

    monkeypatch.setattr(web_app, "read_document_inputs", read)
    monkeypatch.setattr(web_app, "build_search", build)

    def request():
        ready.wait(timeout=10)
        search = web_app.search_index(doc)
        web_app.checklist_facts(doc)
        return search

    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(request) for _ in range(3)]
        try:
            assert entered.wait(10)
        finally:
            release.set()
        results = [job.result(timeout=10) for job in jobs]

    assert len(reads) == len(builds) == 1
    assert results[0] is results[1] is results[2]
    save_evidence("重新导入后更新的资格条件")
    new_search = web_app.search_index(doc)
    _, (rows, _) = web_app.comparison_files(doc)
    web_app.checklist_facts(doc)
    assert new_search is not results[0]
    assert rows[0]["text"] == "重新导入后更新的资格条件"
    assert len(reads) == len(builds) == 2

    writer.add_blank_page(200, 200)
    writer.write(folder / "document.pdf")
    assert len(web_app.search_index(doc).pdf_pages) == 2
    assert len(reads) == len(builds) == 3


def test_cache_retries_failed_build():
    calls = []

    @web_app.cached_build(maxsize=2)
    def build(version):
        calls.append(version)
        if len(calls) == 1:
            raise OSError("临时读取失败")
        return version

    with pytest.raises(OSError):
        build(1)
    assert build(1) == build(1) == 1
    assert calls == [1, 1]


def test_terminal_import_state_survives_windows_replace_failure(tmp_path, monkeypatch):
    from pathlib import Path

    import document_import

    queued = {"id": "fixture", "state": "queued"}
    document_import.save_job(tmp_path, queued)
    replace = Path.replace
    calls = []

    def locked_replace(source, target):
        if target.name == "job.json":
            calls.append(target)
            raise PermissionError("simulated Windows file sharing violation")
        return replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", locked_replace)
        patch.setattr(document_import.time, "sleep", lambda _: None)
        document_import.save_job(tmp_path, {**queued, "state": "failed", "error": "请重试"})
    assert len(calls) == 6
    assert document_import.read_job(tmp_path)["state"] == "failed"
    document_import.save_job(tmp_path, {**queued, "state": "ready"})
    assert document_import.read_job(tmp_path)["state"] == "ready"
