from agentkit import create_app

from .jobs import JOBS

app = create_app("outreach-service", JOBS)
