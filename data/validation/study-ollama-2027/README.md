# Manual validation of extraction (study, seed 20240631)

The sheets labels_A and labels_B of this sample (ids `<eco>-631-NNN`) came back on 2026-10-29,
labelled jointly by two annotators from one group of students at one university (PROTOCOL.md,
section 5 and change log 2026-10-29). The two sheets coincide in all 200 rows; they are one joint
annotation, against which precision and recall are measured. score.json also records Cohen's kappa
over the two identical sheets (1.00), which measures no agreement between independent judgments; the
paper reports none. The extractor is the one that produced every reported number (the version of
2026-10-28); the gaps the labels show are not fixed in it. score.json holds the counts, the error
breakdown and the hashes of the scored sheets; the sheets themselves (labels_*.xlsx, labels_*.csv)
and the sample with its response texts stay with the authors.
