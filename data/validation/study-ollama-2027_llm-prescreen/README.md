# LLM pre-screen of the first study sample (not the manual validation)

The sheets labels_A and labels_B were returned on 2026-10-27 filled by one LLM; their notes say so
and they agree in every row. They are not the two independent human annotations of PROTOCOL.md,
section 5. They served as a pre-screen: the extraction errors they showed were fixed (change log
2026-10-27). The next two samples (seeds 20240629 and 20240630) came back filled by a program too
and became the second and third pre-screens (../study-ollama-2027_llm-prescreen-2 and -3, change log
2026-10-28); the manual validation uses a new sample without the answers of any of them
(data/validation/study-ollama-2027, seed 20240631). prescreen_score.json scores the extractor of
2026-10-26 against these labels.
