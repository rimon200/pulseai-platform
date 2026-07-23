from ai import client

def score_clip(transcript: str) -> int:
    if not transcript.strip():
        return 0

    response = client.responses.create(
        model="gpt-5-nano",
        input=f"""
Rate the viral potential of this gaming clip transcript from 0 to 100.

Consider:
- excitement
- humor
- conflict or drama
- surprising moments
- strong reactions
- whether viewers would share it

Transcript:
{transcript}

Return only a whole number from 0 to 100.
"""
    )

    try:
        score = int(response.output_text.strip())
    except ValueError:
        return 0

    return max(0, min(score, 100))