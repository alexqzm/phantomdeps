#!/bin/sh
# Deterministic build: pdflatex, bibtex, pdflatex x2. Fails on LaTeX errors.
set -e
cd "$(dirname "$0")"
export TZ=UTC   # PDF dates in UTC: no time zone of the build host in the submission
pdflatex -interaction=nonstopmode -halt-on-error main.tex > build1.log
bibtex main > bib.log
pdflatex -interaction=nonstopmode -halt-on-error main.tex > build2.log
pdflatex -interaction=nonstopmode -halt-on-error main.tex > build3.log
