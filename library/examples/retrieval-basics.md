# Retrieval basics

Interview Prep Tool splits each document into passages of about 1,200 characters and turns every passage into a vector
that captures its meaning. When you ask a question, it finds the passages whose vectors are closest to the question
and sends only those, plus your question, to the model.

## Why this matters

- The model answers from your documents instead of guessing.
- Only a handful of passages leave your machine for each question, not whole files.
- If the answer is not in your documents, the model says so or labels the claim as model knowledge.

## Tips

- Ask specific questions. "What loss function does the paper use?" retrieves better than "Tell me about the paper."
- Untick sources you do not want searched before you ask.
- Press Re-index sources after adding or changing files.
