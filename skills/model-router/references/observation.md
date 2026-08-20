# RouteObservationV1

`RouteObservationV1` is the schema and validator contract owned by `deep-model-router`; orchestrators emit observation records, while this plugin validates them and does not emit, store, aggregate, or route observations. Invoke it with:

```
python3 "$SKILL_DIR/scripts/validate_observation.py" --file <obs.json> --root <dir>
python3 "$SKILL_DIR/scripts/validate_observation.py" --file <obs.json> --root <dir> --check-refs
python3 "$SKILL_DIR/scripts/validate_observation.py" --file <obs.json> --root <dir> \
    --check-refs --check-receipts <receipt-dir>
```

Exit `0` means valid, `1` means the record violates an invariant, and `2` means invalid usage. `--check-receipts` requires `--check-refs`. The validator enforces I-JSON, I-STRUCT, I-CONTRACT, I-ACCEPTED, I-OWNER, I-NO-RAW-KEYS, I-STRING, I-NO-DIFF, I-SIZE, I-SUBJECT, I-GRAIN, I-LINK, I-OBS-MODEL, I-ATTEMPT, I-DIGEST, I-GATES, I-REFS, and I-RECEIPTS.

Worked subject hashes:

```text
{"artifact_id":"ep-01","producer":"deep-loop","run_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV"}
→ a1a6ccd20d42089aa5bdacfba8e80f6176f785383be380961597e856b9d3966c
{"artifact_id":null,"producer":"deep-model-router","run_id":"grp-01"}
→ a4639d1b61339690dd157e980e60f93609265f26fec3267a61bec7483b9243f2
```
