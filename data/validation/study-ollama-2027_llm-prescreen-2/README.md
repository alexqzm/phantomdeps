# LLM pre-screen of the second study sample (not the manual validation)

The sheets labels_A and labels_B of the second study sample (seed 20240629, ids `<eco>-629-NNN`)
were returned on 2026-10-28 filled by one program: the two sheets agree in every row, and their
`guide` sheet was edited. They are not the two independent human annotations of PROTOCOL.md, section
5. They served as a second pre-screen: of the five answers they flag, four showed extraction errors
that are now fixed (change log 2026-10-28), and one (`cargo install`, a tool for the developer's
machine) is outside the extraction's scope, which the annotators' guide now states. The next sample
(seed 20240630) came back as an AI pre-check and became the third pre-screen
(../study-ollama-2027_llm-prescreen-3); the manual validation uses a new sample without the answers
of the three (data/validation/study-ollama-2027, seed 20240631). prescreen_score.json scores the
extractor of 2026-10-27 (the one that drew this sample) against these labels; labels_*.csv are the
sheets' rows without the response texts.
