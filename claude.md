# Synthetic Client Training System

Consultant interview preparation tool for Revodata (a Databricks consulting company).
Real consultants practice interviewing AI-generated synthetic clients that behave
like real organizational stakeholders. System evaluates their performance afterward.

## How It Works
1. Consultant receives a brief initial requirement from a synthetic client (e.g., "We need to setup Databricks for xyz reason")
2. Multi-turn conversation — consultant asks questions to uncover requirements, synthetic AI client responds
3. After interview ends, system evaluates and generates feedback report

## Synthetic Client Behavior Rules
- Built on two dimensions: domain alignment (how well it knows its business) and persona alignment (collaborative vs. combative, clear vs. vague responses)
- NEVER volunteer information unless the consultant asks for it
- Only reveal details that match what was asked — never give too much away (Groundedness)
- Track conversation history, don't repeat or contradict (Context Awareness)
- Must not guide the conversation or reveal its AI nature
- Responses should be natural, concise, and conversational — no lists or overly detailed explanations
- For vague or broad questions, provide a general response and request clarification

## Client Maturity Levels
- **Low**: vague answers ("just make it secure"), can't articulate needs, no understanding of Databricks or technical concepts, doesn't know what it wants
- **Medium**: knows what they want partially, mix of specific and vague answers, basic understanding of Databricks concepts
- **High**: specific answers, knows exactly what they want but not how to achieve it, can react to proposals ("that won't work because..."), asks smart questions

## Solution Space Dimensions
The synthetic client has a defined organizational reality across these dimensions:
- Business units and organizational structure
- Environments (dev/test/prod/sandbox/pre-prod)
- Team and group structure
- Data sensitivity / PII / compliance
- Unity Catalog design
- Service principals and automation
- Access control approach
- Data architecture

## Evaluation (post-interview)
Criteria for judging how the consultant interviewed:
- **Solution space coverage**: which dimensions did consultant explore vs. miss?
- **Question quality**: assess against 14 established mistake types (see docs/evaluation/mistake_types.md for full list)
- **Interaction strategy**: did they only ask questions or also propose solutions/directions?
- **Adaptability**: did they adapt to the client's knowledge level?

## Output
Feedback report about consultant performance. One page max.
Structure (could be but can change, just initial idea): **Continue / Stop / Start**
- Continue: good questions, keep asking these
- Stop: questions that were ineffective or counterproductive, with reasons
- Start: questions you should have asked but didn't

Also include: specific mistakes identified (from mistake types), what better questions would have looked like, and what dimensions were missed entirely.

## Tech Stack
- Python
- LangChain and LangGraph for conversation and state management
- Streamlit for UI
- Set up a virtual environment for the project
- Keep it simple — no overengineering to begin with

## Commands
- `pip install -r requirements.txt` to install dependencies
- `streamlit run app.py` to run the UI
- `python -m pytest tests/` to run tests

## Development Approach
- Build incrementally: conversation loop first, then scenarios, then evaluation
- Explain architectural decisions when I ask — I'm learning LangChain and LangGraph 
- First scenario: anonymized waste management company, medium maturity, 
  migrating to Databricks (see docs/scenarios/waste_management_client.md)

## Project Structure
- Keep flat and simple for now
- Separate the synthetic client logic from the evaluation logic
- Reference docs in docs/ folder, not inline