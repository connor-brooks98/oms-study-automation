"""Render synthetic lecture states with production templates; no live app/services.

Run with the existing Python environment. Use --port 0 for a free localhost port.
Fixtures: 1 empty, 2 slides, 3 transcript, 4 both, 5 processing, 6 failure, 7 removed.
Upload/processing responses are simulated here; backend behavior is tested with pytest.
"""

import argparse
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Annotated

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pptx import Presentation
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src/oms_hub/web"
env = Environment(loader=FileSystemLoader(WEB / "templates"), autoescape=select_autoescape())
app = FastAPI()
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
STATES = {
    1: set(),
    2: {"slides"},
    3: {"transcripts"},
    4: {"slides", "transcripts"},
    5: set(),
    6: set(),
    7: set(),
}
BATCHES = {}
REMOVED = {lecture_id: set() for lecture_id in STATES}
REMOVED[7].add("transcripts")
FIXTURES = Path("/tmp/studyhub-material-panel-fixtures")
FIXTURES.mkdir(exist_ok=True)
(FIXTURES / "fixture.txt").write_text("Synthetic lecture transcript. No identifying material.\n")
Presentation().save(str(FIXTURES / "fixture.pptx"))
canvas = Canvas(str(FIXTURES / "fixture.pdf"))
canvas.drawString(72, 720, "Synthetic lecture slides - local Study Hub preview")
canvas.save()


def revision(kind):
    return NS(
        id=1 if kind == "slides" else 2,
        kind=NS(value=kind),
        canonical_derived_path=Path(
            "Fixture lecture slides.pdf" if kind == "slides" else "Fixture cleaned transcript.txt"
        ),
        immutable_source_path=Path(
            "Fixture slides.pptx" if kind == "slides" else "Fixture original transcript.txt"
        ),
    )


@app.get("/lectures/{lecture_id}", response_class=HTMLResponse)
def lecture(request: Request, lecture_id: int):
    request.state.csrf_token = "fixture-only-token"
    states = STATES[lecture_id]
    steps = [
        NS(
            name="slides_received",
            status="running" if lecture_id == 5 else "failed" if lecture_id == 6 else "waiting",
        )
    ]
    item = NS(
        id=lecture_id,
        subject="Fixture course",
        exam_number=1,
        lecture_number=lecture_id,
        topic="Synthetic material panels",
        lecturer="",
        exam_date=None,
        steps=steps,
        passes=[NS(position=i, completed_on=None, resource=None) for i in range(1, 6)],
    )
    return env.get_template("lecture.html").render(
        request=request,
        title=f"Fixture {lecture_id}: material panels",
        lecture=item,
        course_hue=210,
        previous_lecture=NS(id=max(1, lecture_id - 1)),
        next_lecture=NS(id=min(7, lecture_id + 1)),
        slide_revision=revision("slides") if "slides" in states else None,
        transcript_revision=revision("transcripts") if "transcripts" in states else None,
        slide_problem=None if "slides" in states else "not uploaded",
        transcript_problem=None if "transcripts" in states else "not uploaded",
        material_roles={
            "slides": {"pptx", "pdf", "original"} if "slides" in states else set(),
            "transcripts": {"cleaned"} if "transcripts" in states else set(),
        },
        proposed_revisions=[],
        removed_materials=[revision(kind) for kind in sorted(REMOVED[lecture_id])],
        release_steps=steps,
        pass_resources=["Slides", "Transcript"],
        outline_output=None,
    )


@app.post("/uploads/{kind}", status_code=202)
async def upload(
    kind: str, lecture_id: Annotated[int, Form()], files: Annotated[UploadFile, File()]
):
    if kind in REMOVED[lecture_id]:
        return JSONResponse(
            {
                "detail": (
                    "This exact material was removed. Restore the retained material "
                    "on the lecture page, or upload a changed file."
                )
            },
            status_code=409,
        )
    await files.read()
    batch_id = str(len(BATCHES) + 1)
    BATCHES[batch_id] = {"lecture": lecture_id, "kind": kind, "polls": 0}
    return {"batch_id": batch_id}


@app.get("/api/upload-batches/{batch_id}")
def batch(batch_id: str):
    item = BATCHES[batch_id]
    item["polls"] += 1
    complete = item["polls"] > 1
    if complete:
        STATES[item["lecture"]].add(item["kind"])
    return {
        "lifecycle": "terminal" if complete else "active",
        "items": [
            {
                "id": batch_id,
                "state": "complete" if complete else "processing",
                "lecture_id": item["lecture"],
                "kind": item["kind"],
            }
        ],
    }


@app.post("/lectures/{lecture_id}/materials/{revision_id}/{action}")
def change(lecture_id: int, revision_id: int, action: str):
    kind = "slides" if revision_id == 1 else "transcripts"
    if action == "remove":
        STATES[lecture_id].discard(kind)
        REMOVED[lecture_id].add(kind)
    elif action == "restore":
        STATES[lecture_id].add(kind)
        REMOVED[lecture_id].discard(kind)
    return {"ok": True}


@app.get("/artifacts/{revision_id}/cleaned/download")
def download(revision_id: int):
    return FileResponse(FIXTURES / "fixture.txt", filename="Fixture transcript.txt")


@app.get("/artifacts/{revision_id}/original")
def original(revision_id: int):
    return FileResponse(FIXTURES / "fixture.pptx", filename="Fixture slides.pptx")


@app.get("/artifacts/{revision_id}/pdf/download")
def pdf_download(revision_id: int):
    return FileResponse(FIXTURES / "fixture.pdf", filename="Fixture slides.pdf")


@app.get("/artifacts/{revision_id}/pdf")
def pdf_view(revision_id: int):
    return FileResponse(FIXTURES / "fixture.pdf", media_type="application/pdf")


@app.get("/artifacts/{revision_id}/cleaned", response_class=HTMLResponse)
@app.get("/artifacts/{revision_id}/raw", response_class=HTMLResponse)
def text_view(request: Request, revision_id: int):
    return env.get_template("artifact_text.html").render(
        request=request,
        title="Fixture transcript.txt",
        content=(FIXTURES / "fixture.txt").read_text(),
        download_url=f"/artifacts/{revision_id}/cleaned/download",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8769)
    uvicorn.run(app, host="127.0.0.1", port=parser.parse_args().port, access_log=False)
