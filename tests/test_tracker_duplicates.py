from openpyxl import Workbook

from oms_hub.tracker_import import parse_tracker


def test_tracker_duplicate_reports_both_rows_and_keeps_first_value(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Schedule"
    sheet.append(["Neuro 1", None, None])
    sheet.append([1, "First topic", "Dr A"])
    sheet.append([1, "Second topic", "Dr B"])
    path = tmp_path / "tracker.xlsx"
    workbook.save(path)
    workbook.close()

    parsed = parse_tracker(path)

    assert len(parsed.lectures) == 1
    assert parsed.lectures[0].topic == "First topic"
    assert [(issue.row_number, issue.message) for issue in parsed.issues] == [
        (2, "duplicate lecture number also appears at Schedule row 3"),
        (3, "duplicate lecture number first appears at Schedule row 2"),
    ]
