"""Streamlit UI for MedicalRAG."""
import os
import tempfile

import streamlit as st

from medrag.catalog.registry import load_manifest
from medrag.rag.engine import RagEngine

st.set_page_config(page_title="Medical Study Assistant", layout="wide")
st.title("Medical Study Assistant / دستیار مطالعه پزشکی")


@st.cache_resource(show_spinner="Loading models & index…")
def get_engine():
    return RagEngine()


engine = get_engine()


@st.cache_data
def _specialties():
    try:
        specs = sorted({r["specialty"] for r in load_manifest()})
        return ["all"] + specs
    except Exception:
        return ["all"]


with st.sidebar:
    mode = st.radio("Input mode", [
        "Text question",
        "Question image (Persian/English)",
        "Clinical image (ECG/X-ray)",
    ])
    specialty = st.selectbox("Specialty filter", _specialties())
    up = st.file_uploader("Upload image", type=["png", "jpg", "jpeg"]) \
        if mode != "Text question" else None


def _save(upload):
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(upload.name)[1])
    tf.write(upload.read())
    tf.close()
    return tf.name


def _sources(srcs):
    for s in srcs:
        st.markdown(f"- **[{s['n']}]** {s['title']} — p{s['page']} · _{s.get('specialty', '')}_")


if mode == "Question image (Persian/English)":
    if up and st.button("Read & answer", type="primary"):
        p = _save(up)
        st.image(p, width=360)
        with st.spinner("Reading questions & answering…"):
            out = engine.answer_question_image(p)
        with st.expander("OCR'd text"):
            st.text(out["ocr_text"])
        for i, r in enumerate(out["results"], 1):
            st.subheader(f"Question {i}")
            st.markdown(f"> {r['question']}")
            st.markdown(r["answer"])
            _sources(r["sources"])
        os.unlink(p)
else:
    q = st.text_area("Your question:", height=120)
    if st.button("Answer", type="primary") and q.strip():
        img_path = _save(up) if up else None
        if img_path:
            st.image(img_path, width=320)
        with st.spinner("Retrieving & reasoning…"):
            res = engine.answer(q, image_path=img_path, specialty=specialty)
        if res.get("image_desc"):
            with st.expander("Image analysis", expanded=True):
                st.write(res["image_desc"])
        st.subheader("Answer")
        st.markdown(res["answer"])
        st.subheader("Sources")
        _sources(res["sources"])
        if img_path:
            os.unlink(img_path)
