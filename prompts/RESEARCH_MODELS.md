# Research prompt — best small local LLM for the GhostHands brain

Run on Claude Fable 5 (one-shot deep research). Whatever it recommends: paper
ranking ≠ verdict — bench the top-3 on the real decide() loop before swapping.

---

Research the best small local LLMs to be the brain of a macOS computer-use agent.
Primary sources (HuggingFace cards, GitHub, independent evals), cross-check against
r/LocalLLaMA. Ranked actionable picks, not a survey.

## What the model actually does
It is the decision-maker in a snapshot→decide→act loop driving real macOS apps
through the Accessibility (AX) tree. Per turn it gets ~600-700 tokens: a GOAL plus
a digest of numbered UI elements (`[5] AXButton '7'`, `[49] AXLink 'Learn more'`,
current display values). It replies with ONLY compact JSON:
{"plan": "<one sentence>", "done": bool, "clicks": [element_index, ...]}.
No vision, no tool-calling API. Pure text → JSON.

## Capabilities we will eval it on (our new benchmark — design for these, not
toy tasks)
1. LONG ORDERED PLANS: 9+ click sequences where one transposition = wrong result
   (e.g. computing (47+89)×3 on a calculator). Ordering errors are THE small-model
   failure mode.
2. TARGET DISAMBIGUATION: pick the right "Learn more" among 5 near-identical
   elements, keyed by surrounding context in the digest.
3. MULTI-TURN STATE: 3-page form wizards — remember what's already done, don't
   repeat registered actions.
4. HONESTY / TRUST PROBES: when the goal is impossible (the named button doesn't
   exist) the model must say so — NOT hallucinate an index or claim done. We score
   claiming-done-falsely as the worst failure. Models that "helpfully" guess fail.
5. STRATEGY ROUTING + SCENE GENERATION: canvas apps (Excalidraw) have NO AX tree.
   Click-drawing a diagram took 6 minutes per rectangle — total failure. The right
   move is generating an Excalidraw scene-JSON payload (~30 elements: rectangles,
   arrows, labels with sane x/y layout) and injecting it. So the model must ALSO
   handle a second output mode: emit larger (~2-4K token) schema-valid JSON with
   coherent 2D coordinates in one shot. Evaluate JSON-schema adherence at length
   and basic spatial-layout competence, not just short replies.
6. DETERMINISM: identical state at temp 0 → identical correct plan, every time.
   We rejected Qwen3.5-4B purely for flip-flopping here despite better scores.

## Constraints
- Ready 4-bit MLX quant on mlx-community (exact repo IDs required); loads in
  current mlx-lm. Flag architectures whose prompt cache can't trim
  (linear-attention/hybrid layers).
- Hardware: Apple M4 Mac mini, 24GB unified. Latency target: ≤2s warm for the
  short click-plan turns (prefill ~650 tok + gen ~40 tok, KV cache reused);
  scene-JSON generation may take longer but must stay schema-valid throughout.
- ≤ ~9B params. License usable in an MIT project.
- Thinking/CoT models only if thinking fully disables via chat-template flag.

## Baseline to beat
mlx-community/Qwen3-4B-Instruct-2507-4bit (~20 tok/s decode, ~270 tok/s prefill
on the target M4). It saturates our old toy tasks at 100%, so rank candidates on
the six axes above — especially 1, 4, and 5, where small models actually differ.
Already rejected: Qwen2.5-7B (slow, wanders), Qwen3.5-4B (non-deterministic),
Qwen3-1.7B (ordering errors), Phi-4-mini (JSON bugs), LFM2.5-8B-A1B (forced CoT).

## Deliverable
Ranked table: model | repo ID | params/quant | expected decode+prefill tok/s on
base M4 | evidence per axis (1-6, cite or mark unknown) | thinking toggle |
license | verdict. Top-3 shortlist with exact apply_chat_template flags and
gotchas. Separately flag: any model unusually good at LONG structured JSON
(axis 5) even if mid at clicking — we can route canvas tasks to a second model.
Include anything from the last 6 months old threads would miss.
