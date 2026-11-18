"""resume_lint: mechanical defects a recruiter would see, and nothing that could be intended."""
from app.services.resume_lint import lint

CLEAN = """# Jane Doe
Staff Engineer
Pune, India | jane@example.com | https://example.com/portfolio

## Summary
Backend engineer with 10 years building data platforms.

## Experience
### Lead Engineer | Zendrive
Bengaluru, IN | Jun 2024 - Present
- Reduced incidents by 45% across Kafka pipelines.
- Mentored 5 engineers on system design.

## Projects
Knowledge Assistant: Designed RAG pipelines, resulting in 30% faster retrieval.
- Cut hosting cost by 20%.

## Education
B.Tech, Computer Science | Kalyani Government Engineering College | 7.96/10
"""


def codes(markdown: str) -> list[str]:
    return [issue["code"] for issue in lint(markdown)]


def test_a_clean_resume_has_no_findings():
    assert lint(CLEAN) == []


def test_a_fragment_of_the_line_above_is_a_repeat():
    md = CLEAN.replace("- Cut hosting cost by 20%.", "- resulting in 30% faster retrieval.")
    issues = lint(md)
    assert [i["code"] for i in issues] == ["REPEATED_LINE"]
    assert issues[0]["text"] == "resulting in 30% faster retrieval."


def test_the_same_bullet_twice_is_a_repeat():
    md = CLEAN.replace("- Mentored 5 engineers on system design.",
                       "- Reduced incidents by 45% across Kafka pipelines.")
    assert codes(md) == ["REPEATED_LINE"]


def test_short_lines_are_never_called_repeats():
    md = CLEAN.replace("- Cut hosting cost by 20%.", "- Python, Go")
    md = md.replace("Backend engineer with", "Python, Go\nBackend engineer with")
    assert "REPEATED_LINE" not in codes(md)


def test_stray_dashes_and_space_before_punctuation():
    md = CLEAN.replace("- Mentored 5 engineers", "-Mentored 5 engineers").replace("Kafka pipelines.",
                                                                                  "Kafka , pipelines.")
    assert sorted(codes(md)) == ["SPACE_BEFORE_PUNCT", "STRAY_PUNCTUATION"]


def test_the_appended_source_resume_is_not_linted():
    assert lint(CLEAN + "\n## Source Resume\n-raw text , here\n-raw text , here\n") == []
