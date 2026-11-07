import os, json, re
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
from groq import Groq
from dotenv import load_dotenv
from db import fetch_rows
from sql_guard import validate_sql

load_dotenv()
app = FastAPI(title="Student Skills WhatsApp Query Bot")
FRONTEND_DIR = Path(__file__).parent / "frontend"
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
# Groq retired llama-3.3-70b-versatile on 2026-08-16 for free/developer tiers.
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def groq_complete(messages, *, temperature=0, max_tokens=700) -> str:
    """Call Groq and return the model's visible text, including reasoning-model replies."""
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not api_key or api_key in {"your_groq_api_key", "CHANGE_ME"}:
        raise RuntimeError("GROQ_API_KEY is missing. Set a real Groq API key in .env.")
    params = {
        "model": MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if "gpt-oss" in MODEL:
        params["reasoning_effort"] = "low"
    response = groq_client.chat.completions.create(**params)
    message = response.choices[0].message
    content = (message.content or "").strip()
    if content:
        return content
    reasoning = getattr(message, "reasoning", None) or getattr(message, "reasoning_content", None)
    if reasoning and str(reasoning).strip():
        return str(reasoning).strip()
    raise RuntimeError(f"Groq model {MODEL} returned an empty reply.")


VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WA_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
PHONE_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v22.0")

# Session store to track multi-step options per user
USER_SESSIONS = {}
LAST_CONTEXT = {}

STOP_WORDS = {
    "how", "many", "distinct", "students", "student", "cleared", "clear", "passed", "pass",
    "have", "has", "count", "number", "of", "who", "show", "total", "list", "the", "for",
    "in", "what", "is", "are", "which", "give", "me", "tell", "get", "result", "results",
    "course", "courses", "vertical", "verticals", "attempt", "attempts",
    "top", "first", "latest", "most", "overall", "ranked", "ranking", "across", "all",
    "completed", "complete", "named", "names", "name", "their", "them", "those", "these",
    "same", "with", "by", "from", "please", "a", "an", "and", "or", "to", "as",
}

GENERIC_PHRASE = re.compile(
    r"^(?:overall|completed levels?|all courses?|all verticals?|students?|"
    r"names?|named|results?|records?|levels?|top \d+|completed students?)$",
    re.I,
)
FOLLOWUP_RE = re.compile(
    r"\b(their|them|those|these|same|they|the students|list (?:the |their )?names?|"
    r"this course|that course|same course|this level|that level|in this course)\b",
    re.I,
)
DEPT_LABELS = {
    "CS": "CSE", "EC": "ECE", "AD": "AIDS", "IT": "IT", "BT": "Biotech",
    "AL": "AIML", "EE": "EEE", "EI": "EIE", "ME": "Mechanical", "BM": "Biomedical",
    "AG": "Agriculture", "FD": "Fashion", "CE": "Civil", "FT": "Food Technology",
    "CB": "CB", "CD": "CD", "MZ": "MZ", "CT": "CT", "SE": "SE",
}

SCHEMA = """
Approved PostgreSQL table: ps_cleared_course_view
This is the ONLY dataset. It is the imported ps_cleared_data view.
Columns:
roll_number VARCHAR, name VARCHAR, vertical_name VARCHAR, course_name VARCHAR,
date DATE, start_time TIME, end_time TIME, venue_name VARCHAR, status VARCHAR.

DATA FACTS measured from ps_cleared_data.sql:
- 225576 attempt rows, 1451 students, 1527 course_name values, 371 vertical_name values.
- status is exactly one of: 'Cleared', 'Not Cleared', 'Pending for Result'.
- dates run from 2023-12-04 through 2026-10-13. There are 146 venues.
- Skill levels live in course_name, for example 'C Programming Level - 5'
  and 'Programming C++ - Level 5'. Plain "C" means C Programming, not C++.
- Academic subjects use vertical_name for the subject, such as
  '22EC006 - VIRTUAL INSTRUMENTATION IN EMBEDDED SYSTEMS', and course_name
  for a unit of that subject, such as 'UNIT 1-22EC006 - ...'.

COLUMN MEANING:
- roll_number: student id. Characters 8-9 are the department code. There is no
  department column. Use substring(roll_number from 8 for 2). Codes: CS=CSE (204
  students), EC=ECE (191), AD=AIDS (156), IT=IT (154), BT=Biotech (96), AL=AIML
  (95), CB=CB (55), EE=EEE (54), CD=CD (48), MZ=MZ (47), EI=EIE (47),
  ME=Mechanical (46), CT=CT (44), SE=SE (43), BM=Biomedical (43), AG=Agriculture
  (42), FD=Fashion (39), CE=Civil (28), FT=Food Technology (18). When the user
  names one department, filter that code. When the user compares departments,
  GROUP BY the code and do not filter to one department. Never match the
  department word against vertical_name or course_name.
- name: student name. Filter with name ILIKE.
- vertical_name: academic subject, for example '22EC006 - VIRTUAL INSTRUMENTATION...'.
- course_name: one assessment. Either a skill level such as 'C Programming Level - 5'
  or a subject unit such as 'UNIT 1-22EC006 - ...'. Plain "C" means C Programming,
  not C++. Python means Programming Python. Java means Programming Java.
- date, start_time, end_time, venue_name: when and where that attempt happened.
  Dates run from 2023-12-04 through 2026-10-13. There are 146 venues.
- status: exactly 'Cleared', 'Not Cleared', or 'Pending for Result'.
  cleared, passed, and completed mean status ILIKE 'Cleared'.

QUERY RULES FOR ANY QUESTION:
1. Each row is one attempt, not one student. Use only this table and these columns.
2. Student counts: COUNT(DISTINCT roll_number). Attempt counts: COUNT(*).
   A completed level is COUNT(DISTINCT course_name) where status ILIKE 'Cleared'.
3. A clear rate is 100.0 * COUNT(DISTINCT roll_number) FILTER (WHERE status ILIKE 'Cleared')
   / NULLIF(COUNT(DISTINCT roll_number), 0). When ranking groups by a rate,
   require COUNT(DISTINCT roll_number) >= 10 so tiny groups do not rank first.
4. Answer the grouping the user asked for: student, course_name, vertical_name,
   department code, venue_name, status, or date. SELECT the grouped label and the
   metric. ORDER BY the metric they asked about. LIMIT to the top N they asked for.
5. If a resolved course or vertical is provided, filter with the full string:
   (course_name ILIKE '%entity%' OR vertical_name ILIKE '%entity%').
   Do not split that name into separate AND keyword filters.
6. If no course or vertical was resolved, do not invent one from words such as
   overall, students, levels, names, ranked, performance, or department.
7. Only SELECT. Include LIMIT <= 500.
"""

DEPARTMENT_PATTERNS = (
    (r"\b(?:cse|computer science(?: and engineering)?)\b", "CS"),
    (r"\b(?:ece|electronics and communication)\b", "EC"),
    (r"\b(?:information technology|it department|it dept)\b", "IT"),
    (r"\b(?:aids|artificial intelligence and data science|ai & ds|ai and ds)\b", "AD"),
    (r"\b(?:aiml|ai\s*&\s*ml|artificial intelligence and machine learning)\b", "AL"),
    (r"\b(?:eee|electrical and electronics)\b", "EE"),
    (r"\b(?:eie|electronics and instrumentation)\b", "EI"),
    (r"\b(?:mechanical engineering|mechanical department|mech department|mech dept)\b", "ME"),
    (r"\b(?:civil engineering|civil department)\b", "CE"),
    (r"\b(?:biomedical|bme)\b", "BM"),
    (r"\b(?:biotech|biotechnology)\b", "BT"),
    (r"\bagriculture\b", "AG"),
    (r"\bfood technology\b", "FT"),
    (r"\bfashion\b", "FD"),
    (r"\bcs\s+(?:department|dept)\b", "CS"),
    (r"\bec\s+(?:department|dept)\b", "EC"),
    (r"\bad\s+(?:department|dept)\b", "AD"),
    (r"\bal\s+(?:department|dept)\b", "AL"),
    (r"\bee\s+(?:department|dept)\b", "EE"),
    (r"\bei\s+(?:department|dept)\b", "EI"),
    (r"\bme\s+(?:department|dept)\b", "ME"),
    (r"\bbt\s+(?:department|dept)\b", "BT"),
    (r"\b(?:cb|cd|mz|ct|se|fd|ft|ag|ce)\s+(?:department|dept)\b", None),
)


def detect_department(question: str) -> str | None:
    """Reads a department from the question. It is stored inside roll_number."""
    text = re.sub(r"\s+", " ", question.lower())
    for pattern, code in DEPARTMENT_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        if code:
            return code
        return match.group(0).split()[0].upper()
    return None


def apply_department_filter(sql: str, code: str | None) -> str:
    """Forces the roll-number department filter and drops invented department columns."""
    if not code:
        return sql
    sql = re.sub(
        r"\b(?:and\s+)?\w*department\w*\s*(?:=|ILIKE|LIKE)\s*'[^']*'\s*",
        "",
        sql,
        flags=re.I,
    )
    sql = re.sub(
        r"\s+AND\s+(?:vertical_name|course_name)\s+ILIKE\s+'%(?:CSE|CS|ECE)%'",
        "",
        sql,
        flags=re.I,
    )
    sql = re.sub(r"\s+AND\s+AND\s+", " AND ", sql, flags=re.I)
    sql = re.sub(r"\bWHERE\s+AND\s+", "WHERE ", sql, flags=re.I)
    if re.search(r"substring\s*\(\s*roll_number", sql, re.I):
        return sql
    clause = f"substring(roll_number from 8 for 2) = '{code}'"
    limit = re.search(r"\s+LIMIT\s+\d+\s*$", sql, re.I)
    if limit:
        return sql[:limit.start()] + f" AND {clause}" + sql[limit.start():]
    return sql + f" AND {clause}"


def normalize_str(text: str) -> str:
    """Removes non-alphanumeric characters and converts to lowercase for comparisons."""
    lowered = str(text).lower().replace("c++", "cplusplus").replace("c#", "csharp")
    return re.sub(r"[^a-zA-Z0-9]", "", lowered)

def stem_term(term: str) -> str:
    """Strips plural suffixes ('s' or 'es') so singular/plural search terms match."""
    t = term.strip()
    if t.lower().endswith('es') and len(t) > 4:
        return t[:-2]
    if t.lower().endswith('s') and len(t) > 3 and not t.lower().endswith('ss'):
        return t[:-1]
    return t

def extract_entity_phrases(question: str) -> list[str]:
    """Extracts complete database entity phrases, preserving AND/OR groups."""
    prompt = (
        "Extract only course or vertical names from the user question as a JSON array. "
        "Keep level numbers with the course (example: \"C level 5\"). "
        "If the question is overall/global, about a student name/roll, venue, date, "
        "or status, and names no specific course/vertical, return []. "
        "Never extract query language such as overall, students, names, ranked, completed, "
        "levels, across, all courses, top N. Return JSON only.\n"
        f"Question: {question}"
    )
    try:
        raw = groq_complete(
            [{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=400,
        )
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        phrases = json.loads(match.group(0)) if match else []
        if isinstance(phrases, list):
            cleaned = [p for p in split_entity_phrases(phrases) if not GENERIC_PHRASE.match(p)]
            return list(dict.fromkeys(cleaned))
    except Exception as error:
        print(f"Entity extraction failed: {type(error).__name__}: {error}")

    stripped = re.sub(
        r"\b(how many|count|number of|distinct|students?|who|cleared|passed|completed|"
        r"show|list|what is|what are|give me|tell me|the|all|overall|ranked by|"
        r"their names?|named of|across all courses?)\b",
        " ",
        question,
        flags=re.I,
    )
    phrases = re.split(r"\s+(?:and|or)\s+", stripped, flags=re.I)
    return [
        phrase
        for phrase in split_entity_phrases(phrases)
        if phrase and not GENERIC_PHRASE.match(phrase)
    ]

def split_entity_phrases(phrases: list) -> list[str]:
    """Splits conjunction-joined entities returned by the extractor."""
    result = []
    for phrase in phrases:
        parts = re.split(r"\s+(?:and|or)\s+", str(phrase), flags=re.I)
        result.extend(clean_entity_phrase(part) for part in parts)
    return [phrase for phrase in result if phrase]

def clean_entity_phrase(phrase: str) -> str:
    """Removes generic query language while preserving entity words and levels."""
    cleaned = re.sub(r"\s+", " ", phrase).strip(" ?,.")
    cleaned = re.sub(r"^(?:top|first|latest|most)\s+\d+\s+", "", cleaned, flags=re.I)
    while True:
        updated = re.sub(
            r"^(?:(?:top|first|latest|most)\s+\d+|how|many|distinct|student|students|cleared|clear|passed|pass|"
            r"have|has|count|number|of|who|show|total|list|the|for|in|what|is|"
            r"are|which|give|me|tell|get|result|results|course|courses|vertical|"
            r"verticals|attempt|attempts|top|first|latest|most)\s+",
            "",
            cleaned,
            flags=re.I,
        )
        if updated == cleaned:
            break
        cleaned = updated
    return re.sub(
        r"\s+(?:completed|complete|students?|names?|named|overall|levels?)$",
        "",
        cleaned,
        flags=re.I,
    ).strip(" ?,.")


def score_entity_match(phrase: str, name: str) -> int:
    """Ranks how closely a course or vertical name matches the requested phrase."""
    tokens = [
        normalize_str(stem_term(term))
        for term in re.findall(r"[a-zA-Z]+|\d+", phrase)
        if term.lower() not in STOP_WORDS
    ]
    if not name or not tokens:
        return 0
    norm_name = normalize_str(name)
    if not all(token in norm_name for token in tokens):
        return 0
    score = 200 + (300 if normalize_str(phrase) in norm_name else 0)
    raw = phrase.lower()
    if re.search(r"\bc\b", raw) and "c++" not in raw and "cpp" not in raw:
        if "cplusplus" in norm_name:
            score -= 120
        if "cprogramming" in norm_name:
            score += 120
    return score


_CATALOG = None


def get_catalog() -> list[str]:
    """Loads every real course and vertical name from ps_cleared_data once."""
    global _CATALOG
    if _CATALOG is None:
        _, courses = fetch_rows("SELECT DISTINCT course_name FROM ps_cleared_course_view")
        _, verticals = fetch_rows("SELECT DISTINCT vertical_name FROM ps_cleared_course_view")
        _CATALOG = [name for (name,) in (*courses, *verticals) if name]
    return _CATALOG


def lookup_entity_matches(phrase: str) -> list[str]:
    """Finds the closest real course or vertical names for one phrase."""
    if detect_department(phrase) and not re.search(r"\b(level|programming|unit)\b", phrase, re.I):
        return []
    scored = sorted(
        ((score_entity_match(phrase, name), name) for name in get_catalog()),
        key=lambda item: (-item[0], item[1] or ""),
    )
    scored = [(score, name) for score, name in scored if score > 0]
    if not scored:
        return []
    best = scored[0][0]
    close = [name for score, name in scored if score >= best - 30]
    if len(close) == 1 or (len(scored) > 1 and scored[0][0] - scored[1][0] >= 40):
        return [scored[0][1]]
    return close[:12]


def resolve_vertical_or_course(question: str) -> dict:
    """Resolves requested courses or verticals. Unmatched wording still queries the table."""
    phrases = extract_entity_phrases(question)
    if not phrases:
        return {"status": "proceed", "resolved_term": None}

    resolved = []
    ambiguous_groups = []
    for phrase in phrases:
        matches = lookup_entity_matches(phrase)
        if len(matches) == 1:
            resolved.append(matches[0])
        elif len(matches) > 1:
            ambiguous_groups.append(matches)

    if ambiguous_groups:
        return {
            "status": "ambiguous",
            "options": ambiguous_groups[0],
            "pending_options": ambiguous_groups[1:],
            "resolved_entities": resolved,
        }
    return {"status": "proceed", "resolved_term": resolved or None}

def get_sub_levels(entity_name: str) -> list[str]:
    """Queries DB to find all distinct course levels or sub-courses under a topic."""
    sql = """
        SELECT DISTINCT course_name
        FROM ps_cleared_course_view
        WHERE vertical_name ILIKE %s OR course_name ILIKE %s
        ORDER BY course_name;
    """
    param = f"%{entity_name}%"
    try:
        cols, rows = fetch_rows(sql, (param, param))
        if not rows:
            return []
        return sorted(list({r[0] for r in rows if r[0]}))
    except Exception as e:
        print(f"Error fetching sub-levels: {e}")
        return []

def format_selection_message(message: str, options: list[str]) -> str:
    lines = [message, "", "Please select an option:"]
    lines.extend(f"*{idx}* - {option}" for idx, option in enumerate(options, 1))
    return "\n".join(lines)

def execute_multi_entity_query(
    question: str, entities: list[str], structured: bool = False
) -> str | dict:
    entity_filter = " OR ".join(
        "(vertical_name ILIKE %s OR course_name ILIKE %s)" for _ in entities
    )
    entity_count = "COUNT(DISTINCT CASE " + " ".join(
        "WHEN (vertical_name ILIKE %s OR course_name ILIKE %s) THEN %s"
        for _ in entities
    ) + " END)"
    filter_params = [
        value for entity in entities
        for value in (f"%{entity}%", f"%{entity}%")
    ]
    count_params = [
        value
        for entity in entities
        for value in (f"%{entity}%", f"%{entity}%", entity)
    ]
    asks_for_count = bool(re.search(
        r"\b(count|counts|how many|number of|total)\b", question, re.I
    ))
    wants_rows = not asks_for_count and bool(re.search(
        r"\b(who|list|show|top|which)\b", question, re.I
    ))
    if wants_rows:
        sql = f"""
            SELECT roll_number, name, {entity_count} AS matched_entities
            FROM ps_cleared_course_view
            WHERE status ILIKE 'Cleared' AND ({entity_filter})
            GROUP BY roll_number, name
            HAVING {entity_count} = {len(entities)}
            ORDER BY matched_entities DESC, name
            LIMIT 100
        """
        params = count_params + filter_params + count_params
    else:
        sql = f"""
            SELECT COUNT(*) AS student_count
            FROM (
                SELECT roll_number
                FROM ps_cleared_course_view
                WHERE status ILIKE 'Cleared' AND ({entity_filter})
                GROUP BY roll_number
                HAVING {entity_count} = {len(entities)}
            ) matched_students
        """
        params = filter_params + count_params
    cols, rows = fetch_rows(sql, tuple(params))
    result = [dict(zip(cols, [str(value) if value is not None else None for value in row])) for row in rows]
    if not result:
        answer = "No students matched all selected entities."
        return {"answer": answer, "columns": [], "rows": []} if structured else answer
    answer = summarize_result(question, entities, result)
    return {"answer": answer, "columns": cols, "rows": result} if structured else answer

def generate_sql(question: str, resolved_entity: str | list[str] = None) -> str:
    """Generates PostgreSQL query using the exact resolved vertical or course name."""
    if resolved_entity:
        entities = [resolved_entity] if isinstance(resolved_entity, str) else resolved_entity
        entity_text = json.dumps(entities, ensure_ascii=False)
        system_content = (
            "You are a PostgreSQL expert. Output ONLY the executable raw SQL query with no explanation or markdown fences.\n"
            f"The complete database entity targets are EXACTLY: {entity_text}.\n"
            "Every target must be included in the query. IMPORTANT: one assessment "
            "row cannot match two different entities. When there are multiple "
            "targets, first filter rows with status ILIKE 'Cleared' and one OR "
            "condition per target, then GROUP BY roll_number and name and use "
            "HAVING COUNT(DISTINCT CASE WHEN entity_condition_1 THEN 'entity_1' "
            "WHEN entity_condition_2 THEN 'entity_2' END) equal to "
            f"{len(entities)}. This finds students who cleared every target across separate "
            "rows. Never put different entity conditions in an AND clause on the "
            "same row. Do not use abbreviated wording or add entities not in the "
            "target list. Keep SQL to the approved table and columns.\n"
            "Use COUNT(DISTINCT roll_number) for student counts.\n"
            + SCHEMA
        )
    else:
        system_content = (
            "You are a PostgreSQL expert. Output ONLY the executable raw SQL query with no explanation or markdown fences.\n"
            "No specific course or vertical was identified. Answer from the full "
            "ps_cleared_course_view table. Do not invent course filters from generic words.\n"
            + SCHEMA
        )

    raw_sql = groq_complete(
        [
            {"role": "system", "content": system_content},
            {"role": "user", "content": question},
        ],
        temperature=0,
        max_tokens=1200,
    )
    return validate_sql(raw_sql)

def execute_and_answer(
    question: str, resolved_entity: str | list[str] = None, structured: bool = False
) -> str | dict:
    """Helper function to execute SQL and format the answer from Groq."""
    if isinstance(resolved_entity, list):
        if len(resolved_entity) > 1:
            return execute_multi_entity_query(question, resolved_entity, structured)
        resolved_entity = resolved_entity[0] if resolved_entity else None
    department = None if re.search(r"\b(departments?|depts?)\b", question, re.I) and not re.search(
        r"\b(cse|ece|eee|eie|aids|aiml|biotech|biomedical|mechanical|civil|agriculture|fashion)\b",
        question, re.I,
    ) else detect_department(question)
    try:
        sql = apply_department_filter(generate_sql(question, resolved_entity), department)
        cols, rows = fetch_rows(sql)
    except Exception as error:
        sql = apply_department_filter(generate_sql(
            f"{question}\nThe previous SQL failed with: {type(error).__name__}: {error}. "
            "Return a corrected SELECT using only ps_cleared_course_view. "
            "Department is substring(roll_number from 8 for 2), not a column.",
            resolved_entity,
        ), department)
        cols, rows = fetch_rows(sql)

    if not rows:
        target_msg = f" for '{resolved_entity}'" if resolved_entity else ""
        answer = f"No matching records found{target_msg}."
        return {"answer": answer, "columns": [], "rows": []} if structured else answer

    result = [
        {column: present_value(column, value) for column, value in zip(cols, row)}
        for row in rows
    ]
    answer = summarize_result(question, resolved_entity, result)
    if structured:
        return {"answer": answer, "columns": cols, "rows": result}
    return answer


def present_value(column: str, value):
    """Shows department codes by their names. Leaves every other value unchanged."""
    if value is None:
        return None
    text = str(value)
    if column.lower() in {"department", "dept"}:
        return DEPT_LABELS.get(text, text)
    return text


def summarize_result(question: str, resolved_entity, result: list[dict]) -> str:
    """Lists the returned cells so every question shows its actual rows."""
    target = resolved_entity if isinstance(resolved_entity, str) else None
    if isinstance(resolved_entity, list):
        target = ", ".join(str(item) for item in resolved_entity)
    where = f" for {target}" if target else ""
    row = result[0]
    if len(result) == 1 and len(row) == 1:
        key, value = next(iter(row.items()))
        return f"**{value}**\n{key.replace('_', ' ')}{where}."
    lines = []
    for index, item in enumerate(result[:40], 1):
        parts = []
        for key, value in item.items():
            if value is None:
                continue
            shown = f"{value}%" if "rate" in key.lower() and not str(value).endswith("%") else value
            parts.append(f"{key.replace('_', ' ')}: {shown}")
        lines.append(f"{index}. " + " | ".join(parts))
    if len(result) > 40:
        lines.append(f"... and {len(result) - 40} more rows in the table.")
    return "\n".join(lines)

def answer_question(sender: str, question: str, structured: bool = False) -> str | dict:
    global USER_SESSIONS, LAST_CONTEXT
    clean_text = question.strip()
    norm_input = normalize_str(clean_text)

    # 1. PROCESS MULTI-STEP SESSION (Topic Selection -> Level Selection -> Execute)
    if sender in USER_SESSIONS:
        session = USER_SESSIONS[sender]
        step = session.get("step", "SELECT_TOPIC")
        options = session.get("options", [])
        orig_question = session.get("original_question", question)

        selected_option = None

        # Check numeric selection (e.g., "1", "2")
        num_match = re.search(r"^\s*(?:option\s*)?(\d+)\s*$", clean_text, re.I)
        if num_match:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(options):
                selected_option = options[idx]

        # Check text matching selection
        if not selected_option:
            for opt in options:
                if norm_input == normalize_str(opt):
                    selected_option = opt
                    break

        if selected_option:
            # STEP 1: TOPIC SELECTED -> CHECK FOR SUB-LEVELS
            if step == "SELECT_TOPIC":
                pending_options = session.get("pending_options", [])
                if pending_options:
                    USER_SESSIONS[sender] = {
                        "step": "SELECT_TOPIC",
                        "options": pending_options[0],
                        "pending_options": pending_options[1:],
                        "multi_entity": True,
                        "resolved_entities": [
                            *session.get("resolved_entities", []),
                            selected_option,
                        ],
                        "original_question": orig_question,
                    }
                    return format_selection_message(
                        "Now select the matching option for the next requested entity.",
                        pending_options[0],
                    )
                if session.get("multi_entity"):
                    del USER_SESSIONS[sender]
                    targets = list(dict.fromkeys([
                        *session.get("resolved_entities", []),
                        selected_option,
                    ]))
                    return execute_and_answer(orig_question, targets, structured)

                sub_courses = get_sub_levels(selected_option)

                # Find sub-courses/levels distinct from the topic itself
                distinct_levels = [
                    c for c in sub_courses
                    if normalize_str(c) != normalize_str(selected_option)
                ]

                if len(distinct_levels) >= 1:
                    level_options = ["All Levels (Overall Count)"] + sub_courses
                    level_options = list(dict.fromkeys(level_options))

                    USER_SESSIONS[sender] = {
                        "step": "SELECT_LEVEL",
                        "selected_topic": selected_option,
                        "options": level_options,
                        "resolved_entities": session.get("resolved_entities", []),
                        "original_question": orig_question
                    }

                    msg = f"Which level of *{selected_option}* are you referring to?\n\n"
                    for idx, opt in enumerate(level_options, 1):
                        msg += f"*{idx}* - {opt}\n"
                    return msg

                # No sub-levels exist -> execute query directly for selected topic
                del USER_SESSIONS[sender]
                entities = session.get("resolved_entities", [])
                targets = list(dict.fromkeys([*entities, selected_option]))
                return execute_and_answer(orig_question, targets, structured)

            # STEP 2: LEVEL SELECTED -> EXECUTE FINAL QUERY
            elif step == "SELECT_LEVEL":
                selected_topic = session.get("selected_topic", selected_option)
                del USER_SESSIONS[sender]

                if "all levels" in selected_option.lower():
                    selected_target = selected_topic
                else:
                    selected_target = selected_option
                entities = session.get("resolved_entities", [])
                targets = list(dict.fromkeys([*entities, selected_target]))
                return execute_and_answer(orig_question, targets, structured)

        del USER_SESSIONS[sender]

    # 2. PROCESS NEW QUESTION
    resolution = resolve_vertical_or_course(question)
    last = LAST_CONTEXT.get(sender, {})
    if (
        resolution.get("status") == "proceed"
        and not resolution.get("resolved_term")
        and last.get("entities")
        and FOLLOWUP_RE.search(question)
    ):
        resolution["resolved_term"] = last["entities"]

    if resolution["status"] == "ambiguous":
        options = resolution["options"]
        USER_SESSIONS[sender] = {
            "step": "SELECT_TOPIC",
            "options": options,
            "pending_options": resolution.get("pending_options", []),
            "multi_entity": bool(resolution.get("pending_options")),
            "resolved_entities": resolution.get("resolved_entities", []),
            "original_question": question
        }
        
        return format_selection_message(
            "Multiple matching topics/verticals were found.",
            options,
        )

    resolved_entity = resolution.get("resolved_term")
    result = execute_and_answer(question, resolved_entity, structured)
    LAST_CONTEXT[sender] = {"entities": resolved_entity, "question": question}
    return result

async def send_whatsapp(to: str, body: str):
    if not (WA_TOKEN and PHONE_ID):
        raise RuntimeError("WhatsApp Cloud API credentials missing")
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp", "recipient_type": "individual",
        "to": to, "type": "text", "text": {"preview_url": False, "body": body[:4000]}
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers={"Authorization": f"Bearer {WA_TOKEN}"}, json=payload)
        r.raise_for_status()

class QueryRequest(BaseModel):
    question: str
    session_id: str


@app.get("/")
def frontend():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Student Skills WhatsApp Query Bot",
        "model": MODEL,
        "groq_key_configured": bool(
            (os.getenv("GROQ_API_KEY") or "").strip()
            and os.getenv("GROQ_API_KEY") not in {"your_groq_api_key", "CHANGE_ME"}
        ),
    }


@app.post("/api/query")
def query(request: QueryRequest):
    question = request.question.strip()
    session_id = request.session_id.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    if not session_id:
        raise HTTPException(status_code=400, detail="Session ID is required.")
    try:
        result = answer_question(session_id, question, structured=True)
        if isinstance(result, dict):
            return result
        return {"answer": result, "columns": [], "rows": []}
    except Exception as error:
        print(f"Frontend query failed: {type(error).__name__}: {error}")
        raise HTTPException(
            status_code=500,
            detail="The question could not be processed. Please try again.",
        ) from error

@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Webhook verification failed")

@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.json()
    try:
        value = body["entry"][0]["changes"][0]["value"]
        messages = value.get("messages", [])
        if not messages:
            return {"status": "ignored"}
        msg = messages[0]
        sender = msg.get("from")
        text_body = msg.get("text", {}).get("body", "").strip()
        if not sender or not text_body:
            return {"status": "ignored"}
        try:
            answer = answer_question(sender, text_body)
        except Exception as e:
            print(f"Question handling failed: {type(e).__name__}: {e}")
            answer = "I couldn't process that query. Please try asking again."
        await send_whatsapp(sender, answer)
    except Exception as e:
        print(f"Webhook processing failed: {type(e).__name__}: {e}")
    return {"status": "received"}