# LLM pre-screen of the third study sample (not the manual validation)

The sheets labels_A and labels_B of the third study sample (seed 20240630, ids `<eco>-630-NNN`)
were returned on 2026-10-28 as an automatic pre-check by an AI: the first line of each `guide`
sheet says so (in Russian) and adds that it does not replace the annotation by two people. The two
files were written two seconds apart by one program and agree in 199 of 200 rows. They are not the
two independent human annotations of PROTOCOL.md, section 5. They served as a third pre-screen: of
the six answers they flag, five showed extraction errors that are now fixed (change log
2026-10-28, third pre-screen): entries named by a `component` field of a `dependencies` list (two
answers), a `dependencies` list inside an entry of a `components:` list, `pip install .` (a
dependency by path), and a `pip install git+...` command line in a shell block that was read as a
requirement line. The sixth flag, on sheet B only, takes keys such as `stm32g4xx-hal::gpio` in a
`[dependencies]` table for declarations; TOML does not allow `::` in an unquoted key, so these lines
declare nothing, which the annotators' guide now states. The manual validation uses a new sample
without the answers of the three pre-screens (data/validation/study-ollama-2027, seed 20240631).
prescreen_score.json scores the extractor of 2026-10-28 that drew this sample (v3.1) against sheet
A (`other`: sheet B) and records the received files' hashes; labels_*.csv are the sheets' rows
without the response texts.
