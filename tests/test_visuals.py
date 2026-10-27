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
        source |= {w.split("'")[0] for w in source} | {"s"}  # "week's" is made of the words "week" and "s"
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


def test_next_time_and_last_week_are_not_steps():
    n = note(("Bread", ["First feed the starter.", "Then mix the dough.", "Finally bake it.",
                        "The starter must be active.", "Next time I will try rye flour."]),
             ("B", ["x"]), ("C", ["y"]))
    flow = by_kind(n)["flow"]
    assert "rye" not in flow["alt"] and "must be active" not in flow["alt"]
    assert "Process in 3 steps" in flow["alt"]
    assert visuals._CUE.match("Last week we met.") is None and visuals._CUE.match("First time I tried it.") is None
    assert visuals._CUE.match("Last, close the lid.") and visuals._CUE.match("Next, add salt.")


def test_amounts_inside_process_steps_do_not_make_a_chart():
    n = note(("Bread", ["First rest the dough for 30 minutes.", "Then fold it every 20 minutes.",
                        "Finally bake it for 45 minutes."]), ("B", ["x"]), ("C", ["y"]))
    assert "chart" not in by_kind(n)
    assert "flow" in by_kind(n)


def test_mind_map_leaves_prefer_points_that_stand_alone():
    n = note(("Feedback", ["Support gets 40 tickets per week.", "That is something we should fix.",
                           "The export is too slow.", "Customers asked for dark mode."]),
             ("B", ["x"]), ("C", ["y"]))
    mm = visuals.mindmap_data(n)
    leaves = mm["branches"][0]["leaves"]
    assert "That is something we should fix" not in leaves and len(leaves) == 3


# ------------------------------------------------------------------ timeline

def dated(*bullets, actions=None):
    n = note(("Plan", list(bullets)), ("B", ["x one"]), ("C", ["y two"]))
    if actions:
        n["actions"] = actions
    return n


def test_timeline_needs_three_dated_items():
    assert visuals.timeline_data(dated("Finish the page by Friday.", "Send the copy on March 15th.")) is None
    assert "timeline" not in by_kind(NOTES) and "timeline" not in by_kind(RICH_NOTES)
    three = dated("Finish the page by Friday.", "Send the copy on March 15th.", "Review the budget next week.")
    tl = visuals.timeline_data(three)
    assert [i["when"] for i in tl["items"]] == ["Friday", "March 15th", "Next week"]
    assert "timeline" in by_kind(three)


def test_timeline_labels_hold_the_task_without_the_date():
    tl = visuals.timeline_data(dated("Design finishes the landing page by Friday.", "On March 15th, marketing sends the newsletter.",
                                     "Legal review is due before 2 April 2027."))
    assert [i["what"] for i in tl["items"]] == ["Design finishes the landing page", "Marketing sends the newsletter", "Legal review"]


def test_timeline_is_sorted_only_when_every_item_has_a_calendar_date():
    n = dated("Launch on 2 April 2027.", "Freeze the content on March 15, 2027.", "Plan the party on 2027-01-10.")
    tl = visuals.timeline_data(n)
    assert tl["by_date"] and [i["when"] for i in tl["items"]] == ["2027-01-10", "March 15, 2027", "2 April 2027"]
    assert by_kind(n)["timeline"]["caption"] == "In date order."
    mixed = dated("Launch on 2 April 2027.", "Freeze the content on Friday.", "Plan the party on 2027-01-10.")
    tl = visuals.timeline_data(mixed)
    assert not tl["by_date"] and [i["when"] for i in tl["items"]] == ["2 April 2027", "Friday", "2027-01-10"]
    assert by_kind(mixed)["timeline"]["caption"] == "In the order they were mentioned."


def test_ambiguous_numeric_dates_keep_the_reading_order():
    n = dated("Call Anna on 03/04/2026.", "Pay the bill on 25/03/2026.", "Book the room on 01/02/2026.")
    tl = visuals.timeline_data(n)
    assert not tl["by_date"] and [i["when"] for i in tl["items"]][0] == "03/04/2026"


def test_habits_possessives_and_plain_words_are_not_deadlines():
    n = dated("We meet every Monday morning.", "Sarah prepares the report for next week's review.",
              "You may want to march on Monday, said nobody.", "Do not forget the 3/4 split.", "Version 1.2.10 is out.")
    found = visuals.timeline_data(n)
    assert found is None or all(i["when"] != "Every Monday" for i in found["items"])
    assert visuals._find_when("We meet every Monday morning.") is None
    assert visuals._find_when("Sarah prepares the report for next week's review.") is None
    assert visuals._find_when("We may march and August is far.") is None
    assert visuals._find_when("Mail it in May.")[2] == "May"
    assert visuals._find_when("R\u00e9union le lundi.") is None
    assert visuals._find_when("Fini avant lundi.")[2] == "Lundi"


def test_timeline_in_french_and_spanish_with_their_own_wording():
    fr = dated("Le design termine la page d'accueil avant vendredi.", "Le marketing envoie la newsletter le 15 mars.",
               "La revue juridique est pr\u00e9vue d'ici le 2 avril 2027.", "Le budget est valid\u00e9 la semaine prochaine pour nous.")
    fr["title"] = "Plan de lancement du site pour nous"
    fr["summary"] = "Le site sera lanc\u00e9 bient\u00f4t et il faut que nous soyons pr\u00eats pour les clients."
    v = by_kind(fr)["timeline"]
    assert v["title"] == "Dates et \u00e9ch\u00e9ances" and "15 mars" in v["alt"]
    assert by_kind(fr)["mindmap"]["title"] == "Carte mentale"
    es = dated("El dise\u00f1o termina para el viernes.", "El correo sale el 15 de marzo de 2027.", "Entregamos el informe el mes que viene.")
    assert [i["when"] for i in visuals.timeline_data(es)["items"]] == ["Viernes", "15 de marzo de 2027", "El mes que viene"]
    assert visuals._find_when("Las pruebas empiezan por la ma\u00f1ana.") is None
    assert visuals._find_when("Las pruebas empiezan ma\u00f1ana.")[2] == "Ma\u00f1ana"


def test_timeline_reads_actions_too_and_skips_duplicates():
    n = dated("Pay the invoice by Friday.", actions=["Pay the invoice by Friday", "Call the venue by tomorrow", "Order seeds on 2026-11-30"])
    tl = visuals.timeline_data(n)
    assert [i["what"] for i in tl["items"]] == ["Pay the invoice", "Call the venue", "Order seeds"]


def test_timeline_is_capped_and_layouts_are_consistent():
    n = dated(*[f"Task number {i} is due on {i + 1} March 2027." for i in range(12)])
    tl = visuals.timeline_data(n)
    assert len(tl["items"]) == visuals.MAX_TIMELINE
    v = by_kind(n)["timeline"]
    assert set(v["layouts"]) == {"wide", "narrow"}
    for name, width in (("wide", visuals.WIDE), ("narrow", visuals.NARROW)):
        lay = v["layouts"][name]
        assert lay["w"] == width and lay["h"] > 0
        rects = [i for i in lay["items"] if i["t"] == "rect"]
        texts = [i for i in lay["items"] if i["t"] == "text"]
        assert len(rects) == 2 * visuals.MAX_TIMELINE
        for r in rects:
            assert r["x"] >= 0 and r["x"] + r["w"] <= width and r["y"] >= 0 and r["y"] + r["h"] <= lay["h"]
        for t in texts:  # every line of text sits inside one of the boxes
            w = visuals.text_width(t["text"], t["size"], t["bold"])
            assert any(r["x"] <= t["x"] and t["x"] + w <= r["x"] + r["w"] and r["y"] <= t["y"] - t["size"] * 0.8 and t["y"] <= r["y"] + r["h"]
                       for r in rects), t["text"]


def test_long_dates_and_tasks_are_wrapped_inside_the_timeline_boxes():
    n = dated("The extraordinarily long task description keeps going " + "and going " * 20 + "on 15th of September 2027.",
              "Another one on 1 October 2027.", "A third one on 3 November 2027.")
    v = by_kind(n)["timeline"]
    for lay in v["layouts"].values():
        rects = [i for i in lay["items"] if i["t"] == "rect"]
        for t in (i for i in lay["items"] if i["t"] == "text"):
            w = visuals.text_width(t["text"], t["size"], t["bold"])
            assert any(r["x"] <= t["x"] and t["x"] + w <= r["x"] + r["w"] for r in rects), t["text"]


# --------------------------------------------------------- wording per language

def test_english_wording_is_unchanged():
    v = by_kind(RICH_NOTES)
    assert (v["mindmap"]["title"], v["flow"]["title"], v["chart"]["title"]) == ("Mind map", "Process", "Key figures")
    assert v["chart"]["caption"] == "Values mentioned in the note, as amounts in euros."


def test_chosen_language_decides_the_wording_even_for_short_notes():
    v = by_kind_lang(RICH_NOTES, "fr")
    assert v["mindmap"]["title"] == "Carte mentale" and v["flow"]["title"] == "Processus" and v["chart"]["title"] == "Chiffres cl\u00e9s"
    assert v["flow"]["caption"] == "Launch steps"  # the section heading is the notes' own text
    assert v["flow"]["alt"].startswith("Processus en 3")


def by_kind_lang(notes, lang):
    return {v["kind"]: v for v in visuals.build_visuals(notes, lang)}


def test_units_in_values_and_captions_follow_the_language():
    n = note(("Temps", ["Le montage prend 5 heures.", "Les tests prennent 12 heures.", "La revue prend 3 heures."]),
             ("B", ["x"]), ("C", ["y"]))
    chart = visuals.chart_data(n, "fr")
    assert chart["items"][0]["text"] == "5 heures"
    assert visuals.chart_data(n, "en")["items"][0]["text"] == "5 hours"
    assert "(heures)" in by_kind_lang(n, "fr")["chart"]["caption"]


def test_generic_flow_caption_is_translated():
    spread = note(("A", ["First, open the file."]), ("B", ["Then edit the text."]), ("C", ["Finally save it."]))
    assert by_kind_lang(spread, "en")["flow"]["caption"] == "The steps in the order they were described."
    assert by_kind_lang(spread, "es")["flow"]["caption"] == "Los pasos en el orden en que se describieron."


def test_every_language_has_every_diagram_word():
    from app import languages

    for code in languages.NATIVE_NAMES:
        for key in languages._EN:
            assert languages.text(key, code), (code, key)
