# A2UI System Overview

One agent, one socket, real answers. A message comes in over WebSocket; an
LLM-driven agent decides what's needed, reaches out to real services for
real data, and streams the result back as live, native UI -- not a wall of
text.

Full interactive version (with the diagrams below rendered as inline SVG):
<https://claude.ai/code/artifact/f4ecfc78-0ddd-468c-bf38-c2f06137d8a1>

## 1. Four stages, one loop

Everything the extension does happens between a client opening a socket
and that same socket receiving back native UI, not a chat transcript.
Nothing is faked or remembered from training data -- every card, map pin,
and price the agent shows came back from a real call made a few hundred
milliseconds earlier.

```mermaid
flowchart LR
    Client["Client<br/>HarmonyOS / Flutter<br/>AGenUI renderer"]
    Agent["Agent<br/>ReAct loop<br/>decides what's needed"]
    Tools["Tools<br/>19 real actions<br/>search, book, render"]
    UI["UI Renderer<br/>genui.py<br/>builds native surfaces"]
    World["The real world<br/>an LLM to reason with, plus live search / maps /<br/>video / shopping data and a browser to fall back on"]

    Client --> Agent --> Tools --> UI
    UI -. "streamed live — cards, maps, forms render as they're ready" .-> Client
    Agent -. reasoning .-> World
    Tools -. "tool calls" .-> World
```

## 2. From message to rendered answer

Not every message needs all six steps -- "hello" skips straight to step 6.
A trip-planning request runs the middle loop several times, once per
place, flight, or hotel involved. The loop over steps 2-4 is the whole
point of a ReAct agent over a plain chat completion: the model can ask for
more real information before committing to an answer, instead of guessing
once and hoping.

```mermaid
flowchart LR
    M1(["1. Message<br/>arrives on the socket"])
    M2(["2. Reason<br/>agent asks the LLM"])
    M3(["3. Call a tool<br/>real search, map, or card"])
    M4(["4. Get result<br/>live data or built UI back"])
    M5(["5. Stream<br/>piece by piece, live"])
    M6(["6. Render<br/>client shows the answer"])

    M1 --> M2 --> M3 --> M4 --> M5 --> M6
    M4 -. "repeats once per tool the agent needs" .-> M2
```

## 3. The four guarantees

| | | |
|---|---|---|
| **Input** | One socket | No REST endpoints, no polling -- a single WebSocket carries every message in both directions. |
| **Reasoning** | One agent | A single ReAct agent handles every request type -- no routing between separate bots per domain. |
| **Action** | Real services only | Every tool hits a real API or a real page -- nothing in the response is invented by the model. |
| **Output** | Native, not text | Results become native cards, maps, and forms on the client, not a block of markdown. |

---

See [`ARCHITECTURE_BLUEPRINT.md`](./ARCHITECTURE_BLUEPRINT.md) for the
wire-level detail this overview leaves out.
