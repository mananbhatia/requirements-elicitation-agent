from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

token = os.environ.get("DATABRICKS_TOKEN")
base_url = os.environ.get("DATABRICKS_BASE_URL")

if not token:
    raise EnvironmentError("DATABRICKS_TOKEN is not set. Add it to your .env file.")
if not base_url:
    raise EnvironmentError("DATABRICKS_BASE_URL is not set. Add it to your .env file.")

client = OpenAI(api_key=token, base_url=base_url)

response = client.chat.completions.create(
    model="databricks-gpt-oss-120b",
    messages=[{"role": "user", "content": "Say hello and tell me what model you are."}],
    max_tokens=5000,
    extra_body={"reasoning_effort": "high"},
)

content = response.choices[0].message.content
if isinstance(content, list):
    for block in content:
        if block.get("type") == "text":
            print(block["text"])
else:
    print(content)
