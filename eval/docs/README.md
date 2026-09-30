# Docs

Design documents (Claude Docs):

- Research & Technical Proposal: https://claude.ai/code/artifact/3631195e-8273-4835-9ae5-522baf89d3dd
- Architecture: https://claude.ai/code/artifact/414b52c0-79ea-4a91-8fbc-684d14076d11
- Implementation Milestones: https://claude.ai/code/artifact/8581d508-2073-4967-a17f-b5cff4094e2f
- Fine-Tuned Models (optional, later): https://claude.ai/code/artifact/1ca06093-b13d-4ee8-b0e3-1309af669861
- Retrieval and Prompt Path (first task): https://claude.ai/code/artifact/1e1ef77f-85f9-494e-953e-5081aa8ba80e

## Running an eval

```bash
python eval/runner/run_eval.py lead.qualify eval/sets/lead_qualify.jsonl services/lead-service/app/prompts \
    app.qualify:QualifyResult --min 0.85
```

Sets are JSONL (`{"input": ..., "expected": {...}}`); start with 40 job posts and 40 replies, growing to the Playbook targets (qualify 100, extract 60,
classify 80, 20 each of email, reply and blog drafts). Labelled data must come from the team; none is committed yet.
