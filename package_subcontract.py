"""Resolve explicit package-level subcontracting choices before general clauses."""
import re

from document_compare import comparison_packages
from package_review import PACKAGE, package_number, standard, unique_sources
from risk_review import source_from


QUESTION = re.compile(r'非主体|非关键|非核心|辅助性|能否|是否|可不可以|可以|能不能|允许|能把|禁止|分包吗')
TOPIC = re.compile(r'(?:非主体|非关键性|非核心|辅助性).{0,32}分包')
CORE_WORK = re.compile(r'(?<!非)(?:主体|关键性|核心)(?:工作|业务|服务)')
CHOICE = re.compile(r'[☑✓√]\s*(不允许|允许|禁止|不可以|可以)')
DIRECT = re.compile(r'^\s*:?\s*(不允许|允许|禁止|不得|不可以|可以)(?:\s*(?:合同)?分包|.{0,24}工作分包|[。;；]|$)')
GENERAL = re.compile(r'未经.{0,16}同意.{0,35}分包|经.{0,16}同意.{0,24}分包')
OTHER_TOPIC = re.compile(r'履行期限|合同(?:期限|履行|多久|何时)|服务(?:期|期限)|有效期|截止|保证金|限价|预算|联合体|评分|资格|投标文件(?:份数|递交)')


def subcontract_question(question):
    return '分包' in question and bool(QUESTION.search(question)) and not CORE_WORK.search(question)


def standalone_subcontract_question(question):
    return subcontract_question(question) and not OTHER_TOPIC.search(question)


def answer_subcontract(question, units, previous_question=''):
    selected = comparison_packages(question) or comparison_packages(previous_question)
    decisions = {}
    scoped_sources = []
    general_sources = []
    for unit in units:
        text = standard(unit['text'])
        if GENERAL.search(text) and unit.get('row') is None and unit.get('type') != 'table':
            general_sources.append(source_from(unit))
        project_row = unit.get('row') is not None and '分包要求' in text.split(' | ')[:-1]
        if not (TOPIC.search(text) or project_row) or (unit.get('type') == 'table' and unit.get('row') is None):
            continue
        markers = list(PACKAGE.finditer(text))
        if not markers and not selected and unit.get('row') is not None:
            option_cell = text.rsplit('|', 1)[-1].strip()
            if CHOICE.match(option_cell) or project_row and CHOICE.search(option_cell):
                source = source_from(unit)
                scoped_sources.append(source)
                for match in CHOICE.finditer(option_cell):
                    allowed = match[1] in {'允许', '可以'}
                    condition = re.split(r'[□☐☑✓√]', option_cell[match.end():], maxsplit=1)[0].strip(' ,，:：;；。') if allowed else ''
                    decisions.setdefault(0, {}).setdefault(allowed, []).append((source, condition))
        for index, marker in enumerate(markers):
            package = package_number(marker)
            if selected and package not in selected:
                continue
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            clause = text[marker.end():end].strip()
            source = source_from(unit)
            scoped_sources.append(source)
            checked = list(CHOICE.finditer(clause))
            choices = [match[1] for match in checked]
            if not choices and (match := DIRECT.match(clause)) and not re.search(r'□|☐', clause):
                choices = [match[1]]
            for choice in choices:
                allowed = choice in {'允许', '可以'}
                condition = ''
                if allowed:
                    option = next((match for match in checked if match[1] == choice), DIRECT.match(clause))
                    condition = re.split(r'[□☐☑✓√]', clause[option.end():], maxsplit=1)[0].strip(' ,，:：;；。')
                decisions.setdefault(package, {}).setdefault(allowed, []).append((source, condition))

    if not decisions or any(len(values) != 1 for values in decisions.values()) or selected - decisions.keys():
        return {
            'status': 'needs_review',
            'reason': '标包专门条款缺失、未勾选或互相冲突，无法确认是否允许分包。请核对招标文件前附表与合同条款。',
            'tool_evidence': unique_sources(scoped_sources + general_sources)[:8],
        }
    answers, sources = [], []
    for package, values in sorted(decisions.items()):
        allowed, references = next(iter(values.items()))
        scope = f'采购包 {package}' if package else '本项目'
        answers.append(f'{scope}{"允许" if allowed else "不允许"}分包非主体、非关键性工作。')
        if allowed:
            conditions = list(dict.fromkeys(condition for _, condition in references if condition))
            answers.extend(f'分包要求：{condition.rstrip("。")}。' for condition in conditions)
            if not conditions:
                answers.append('当前标包条款未列明具体分包条件，请结合原文核对。')
        sources.extend(source for source, _ in references)
    if not selected and 0 not in decisions:
        answers.append('以上仅适用于列出的标包。')
    if general_sources:
        answers.append('合同另有通用分包审批条款，不能据此改变标包专门规定。')
        sources.extend(general_sources[:2])
    return {'status': 'answered', 'answer': ''.join(answers), 'conditions': [],
            'sources': unique_sources(sources)}
