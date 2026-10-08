---
uuid: "3e6e10ce-042a-5774-9004-106af5069efc"
type: "skill"
name: "grill-me"
description: "Stress-test a plan or design through a focused interview. Use when the user asks to be grilled or says \"grill me\"."
---
Interview the user about every aspect of their plan until you reach shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one at a time.

## Questions

Use the harness's structured question tool for every question: AskUserQuestion, request_user_input_async, or request_user_input when available in the current mode. If none is available, ask one concise question in the conversation.

Ask one question at a time. Wait for the user's answer before asking the next. With an asynchronous tool, yield after asking; resume the interview when the answer arrives.

Provide 2–4 concrete choices representing likely answers or directions, within the tool's option limits. Avoid generic Yes/No choices unless the question is binary. Use the tool's built-in custom-answer field; do not add an Other option when the tool supplies it.

## Flow

1. Briefly acknowledge each decision, then ask the next question.
2. Answer questions you can resolve by inspecting the codebase or files yourself.
3. Continue until all branches are resolved, or the user ends or redirects the interview. Do not treat silence or elapsed time as an answer.
4. Finish with a concise summary of the decisions.

Source: https://github.com/RobMitt/grill-me-skill. Adapted for multiple harnesses; interview flow preserved.
