"""fixture Agent 的 internal SDK HTTP 入口。"""
from muye_multi_agent_sdk import create_app

from agent import FixtureKnowledgeAgent


app = create_app(FixtureKnowledgeAgent())
