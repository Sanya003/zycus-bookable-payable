FROM python:3.11-slim

# System deps: tesseract for OCR, poppler for pdftoppm (render.py's preferred
# renderer; PyMuPDF is the in-code fallback if poppler is missing).
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-por \
    tesseract-ocr-est \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# documents/ and master_data/ are expected to be present (either baked into
# the image via COPY above, or mounted at runtime with -v). output/ and
# work/ are written at runtime.
ENTRYPOINT ["python", "-m", "src.run"]
CMD ["documents", "work", "--stage", "all", "--output-dir", "output"]