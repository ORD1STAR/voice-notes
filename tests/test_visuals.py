import re

from app import visuals

from fakes import NOTES, RICH_NOTES


def by_kind(notes):
    return {v["kind"]: v for v in visuals.build_visuals(notes)}


def note(*sections, title="A title"):
    return {"title": title, "summary": "s", "sections": [{"heading": h, "bullets": b} for h, b in sections]}


def all_text(notes):
    parts = [notes["title"]] + [s["heading"] for s in notes["sections"]] + [b for s in notes["sections"] for b in s["bullets"]]
    return " ".join(parts).lower()


def test_small_notes_get_no_diagrams():
    assert visuals.build_visuals(None) == []
    assert visuals.build_visuals(note(("Only", ["One bullet."]))) == []
    assert visuals.build_visuals(note(("A", ["x y z"]), ("B", ["p q r"]))) == []


def test_mind_map_needs_three_sections_and_uses_headings():
    v = by_kind(NOTES)
    assert set(v) == {"mindmap"}
    alt = v["mindmap"]["alt"]
    for sec in NOTES["sections"]:
        assert sec["heading"] in alt
    for layout in v["mindmap"]["layouts"].values():
        center = " ".join(i["text"] for i in layout["items"] if i["t"] == "text" and i["role"] == "center-text")
        assert center == NOTES["title"]


def test_rich_notes_get_all_three_diagrams():
    v = by_kind(RICH_NOTES)
    assert set(v) == {"mindmap", "flow", "chart"}
    assert v["flow"]["caption"] == "Launch steps"
    assert "Freeze the content" in v["flow"]["alt"]
    assert "5,000" in v["chart"]["alt"] or "5000" in v["chart"]["alt"]


def test_flow_steps_drop_the_sequence_words():
    flow = by_kind(RICH_NOTES)["flow"]
    texts = [i["text"] for i in flow["layouts"]["wide"]["items"] if i["t"] == "text" and i["role"] == "step-text"]
    joined = " ".join(texts)
    assert "First" not in joined and "Then" not in joined and "Finally" not in joined
    badges = [i["text"] for i in flow["layouts"]["wide"]["items"] if i["role"] == "badge-text"]
    assert badges == ["1", "2", "3"]


def test_flow_needs_real_sequence_cues():
    plain = note(("A", ["one thing here", "another thing here", "third thing here"]),
                 ("B", ["b one", "b two", "b three"]), ("C", ["c one", "c two", "c three"]))
    assert "flow" not in by_kind(plain)
    french = note(("Procedure", ["D'abord, couper les legumes.", "Ensuite, les faire revenir.", "Enfin, servir chaud."]))
    assert "flow" in by_kind(french)


def test_flow_across_sections():
    spread = note(("A", ["First, open the file."]), ("B", ["Then edit the text."]), ("C", ["Finally save it."]))
    assert "flow" in by_kind(spread)


def test_chart_groups_one_unit_and_needs_three_values():
    two = note(("A", ["Cost is 10 euros.", "Tax is 20 euros."]), ("B", ["x"]), ("C", ["y"]))
    assert "chart" not in by_kind(two)
    mixed = note(("A", ["Cost is 10 euros.", "Share is 20%.", "Margin is 30 dollars."]), ("B", ["x"]), ("C", ["y"]))
    assert "chart" not in by_kind(mixed)
    same_values = note(("A", ["Cost a is 10 euros.", "Cost b is 10 euros.", "Cost c is 10 euros."]))
    assert "chart" not in by_kind(same_values)


def test_chart_number_formats():
    n = note(("A", ["Rent is 1,200 euros.", "Food is 450.5 euros.", "Fun is 90 euros."]))
    items = visuals.chart_data(n)["items"]
    assert [i["value"] for i in items] == [1200.0, 450.5, 90.0]
    assert items[0]["text"] == "€1,200"
    pct = visuals.chart_data(note(("A", ["Up 12% in May.", "Up 7,5 percent in June.", "Down 3% in July."])))
    assert [i["value"] for i in pct["items"]] == [12.0, 7.5, 3.0]
    assert pct["items"][0]["text"] == "12%"


def test_chart_bars_are_proportional():
    chart = by_kind(RICH_NOTES)["chart"]["layouts"]["wide"]
    bars = [i for i in chart["items"] if i["role"] == "bar"]
    assert len(bars) >= 3
    widths = sorted(b["w"] for b in bars)
    assert widths[-1] > widths[0] * 2


def test_nothing_is_invented():
    """Every word shown in a diagram comes from the notes."""
    for notes in (NOTES, RICH_NOTES):
        source = set(re.findall(r"[\w']+", all_text(notes).replace(",", "")))
        source |= {w for w in re.findall(r"[\w']+", " ".join(
            str(x) for s in notes["sections"] for x in s["bullets"]).lower().replace(",", ""))}
        for v in visuals.build_visuals(notes):
            for lay in v["layouts"].values():
                for it in lay["items"]:
                    if it["t"] != "text" or it["role"] in ("badge-text",):
                        continue
                    for w in re.findall(r"[^\W\d_']+", it["text"].lower()):
                        assert w in source or w == "...", (v["kind"], it["text"], w)


def test_layouts_stay_inside_their_canvas_and_text_fits_boxes():
    many = note(*[(f"Topic number {i} with a long heading that needs wrapping", [
        "A rather long first point that keeps going and going to force a wrapped label in the leaf",
        "Second point", "Third point"]) for i in range(10)], title="A very long title " * 5)
    for notes in (NOTES, RICH_NOTES, many):
        for v in visuals.build_visuals(notes):
            for name, lay in v["layouts"].items():
                rects = [i for i in lay["items"] if i["t"] == "rect"]
                for r in rects:
                    assert -0.5 <= r["x"] and r["x"] + r["w"] <= lay["w"] + 0.5, (v["kind"], name, r)
                    assert -0.5 <= r["y"] and r["y"] + r["h"] <= lay["h"] + 0.5, (v["kind"], name, r)
                for t in (i for i in lay["items"] if i["t"] == "text"):
                    w = visuals.text_width(t["text"], t["size"], t["bold"])
                    left = t["x"] - (w / 2 if t["anchor"] == "middle" else 0)
                    assert left >= -0.5 and left + w <= lay["w"] + 0.5, (v["kind"], name, t)
                    boxes = [r for r in rects if r["x"] - 0.5 <= left and left + w <= r["x"] + r["w"] + 0.5
                             and r["y"] <= t["y"] <= r["y"] + r["h"]]
                    if t["role"].endswith("-text") and t["role"] != "badge-text":
                        assert boxes, ("text sticks out of its box", v["kind"], name, t)


def test_mind_map_is_capped():
    big = note(*[(f"Section {i}", [f"Point {i} a", f"Point {i} b", f"Point {i} c"]) for i in range(20)])
    mm = by_kind(big)["mindmap"]
    assert "first 8 of 20" in mm["caption"]
    leaves = [i for i in mm["layouts"]["wide"]["items"] if i["role"] == "leaf"]
    assert len(leaves) <= 8 * 2


def test_wrap_cuts_long_words_and_adds_ellipsis():
    lines = visuals.wrap("word " * 40, 12, 120, 2)
    assert len(lines) == 2 and lines[-1].endswith("...")
    assert all(visuals.text_width(x, 12) <= 120 for x in lines)
    lone = visuals.wrap("x" * 80, 12, 100, 3)
    assert visuals.text_width(lone[0], 12) <= 100


def test_flow_keeps_only_the_sequence_not_the_chatter_around_it():
    n = note(("Garden", ["I wanted to record a note.", "So first we clear the beds.", "Next we add compost.",
                         "Finally we plant the tomatoes.", "The neighbours want some tomatoes."]),
             ("B", ["x"]), ("C", ["y"]))
    flow = by_kind(n)["flow"]
    assert flow["alt"].startswith("Process in 3 steps")
    assert "record a note" not in flow["alt"] and "neighbours" not in flow["alt"]


def test_figures_listed_in_one_sentence_become_separate_bars():
    n = note(("Budget", ["The budget is around 400 euros for the compost, 150 euros for the seeds and "
                         "220 euros for the irrigation parts."]))
    chart = visuals.chart_data(n)
    assert [(i["label"], i["value"]) for i in chart["items"]] == [
        ("The compost", 400.0), ("The seeds", 150.0), ("The irrigation parts", 220.0)]
