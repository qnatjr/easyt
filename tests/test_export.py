import csv
import io

from app.export import CSV_COLUMNS, prompts_csv, script_text


def doc_with(scenes, thumbnail="", negative=""):
    return {
        "id": "demo",
        "scenes": scenes,
        "thumbnail_prompt": thumbnail,
        "negative_prompt": negative,
    }


def scene(**overrides):
    base = {
        "index": 1,
        "start_sec": 0,
        "duration_sec": 2.8,
        "target_sec": 3,
        "narration": "First beat.",
        "shot": "wide establishing shot",
        "focus": "the aqueduct",
        "image_prompt": "a stone aqueduct at dawn",
    }
    return {**base, **overrides}


def rows(csv_string):
    return list(csv.reader(io.StringIO(csv_string)))


def test_header_and_scene_rows():
    out = prompts_csv(
        doc_with(
            [
                scene(),
                scene(
                    index=2,
                    start_sec=2.8,
                    duration_sec=2.4,
                    narration="Second beat.",
                    shot="medium shot",
                ),
            ],
            negative="text, watermark",
        )
    )
    parsed = rows(out)

    assert parsed[0] == CSV_COLUMNS
    assert parsed[1] == [
        "1",
        "00:00:00.0",
        "00:00:02.8",
        "2.8",
        "3",
        "wide establishing shot",
        "the aqueduct",
        "First beat.",
        "a stone aqueduct at dawn",
        "text, watermark",
    ]
    assert parsed[2][1:4] == ["00:00:02.8", "00:00:05.2", "2.4"]
    assert parsed[2][5] == "medium shot"


def test_there_is_no_video_prompt_column():
    assert not any("video" in column for column in CSV_COLUMNS)


def test_the_timeline_is_continuous():
    """Each image starts where the previous one ended — the audio does not pause."""
    parsed = rows(
        prompts_csv(
            doc_with(
                [
                    scene(index=1, start_sec=0, duration_sec=2.8),
                    scene(index=2, start_sec=2.8, duration_sec=2.4),
                    scene(index=3, start_sec=5.2, duration_sec=3.1),
                ]
            )
        )
    )

    ends = [row[2] for row in parsed[1:]]
    starts = [row[1] for row in parsed[1:]]
    assert starts[1:] == ends[:-1]


def test_thumbnail_is_the_final_row_and_carries_the_negative_prompt():
    out = prompts_csv(doc_with([], thumbnail="a bold face, high contrast", negative="blurry"))
    parsed = rows(out)

    assert parsed[-1][0] == "thumbnail"
    assert parsed[-1][-2] == "a bold face, high contrast"
    assert parsed[-1][-1] == "blurry"


def test_no_thumbnail_row_when_there_is_no_prompt():
    assert len(rows(prompts_csv(doc_with([])))) == 1


def test_commas_and_newlines_survive_a_round_trip():
    nasty = 'A prompt with, a comma, "quotes" and\na newline'
    parsed = rows(prompts_csv(doc_with([scene(narration=nasty, image_prompt=nasty)])))

    assert parsed[1][7] == nasty
    assert parsed[1][8] == nasty


def test_every_row_is_the_same_width():
    parsed = rows(prompts_csv(doc_with([scene(), scene(index=2)], thumbnail="t", negative="n")))

    assert {len(row) for row in parsed} == {len(CSV_COLUMNS)}


def test_script_text_is_the_bare_script():
    assert script_text({"script": "  Hello there.  "}) == "Hello there.\n"
    assert script_text({}) == "\n"
