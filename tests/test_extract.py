import pytest

from app.pipeline.extract import ExtractionError, extract_text


def test_extract_pdf(sample_pdf):
    text = extract_text(sample_pdf, ".pdf")
    assert "Photosynthesis" in text
    assert len(text) > 100


def test_extract_txt(sample_txt):
    text = extract_text(sample_txt, ".txt")
    assert "Chlorophyll" in text


def test_extract_image_ocr(sample_image):
    text = extract_text(sample_image, ".png")
    # OCR is imperfect; assert it recovered recognizable content.
    assert any(w in text for w in ("Photosynthesis", "Chlorophyll", "Calvin", "Mitochondria"))


def test_extract_too_short_raises(tmp_path):
    p = tmp_path / "tiny.txt"
    p.write_text("hi")
    with pytest.raises(ExtractionError):
        extract_text(str(p), ".txt")
