import asyncio
from abc import ABC, abstractmethod


class BaseAgent(ABC):
    def __init__(self, agent_type, shared_state):
        self.agent_type = agent_type
        self.state = shared_state
        self.running = True

    async def start(self):
        await self.work_loop()

    async def handle_command(self, command):
        if command['type'] == 'set_intent':
            self.state.set_intent(self.agent_type, command['intent'])
        elif command['type'] == 'pause':
            self.state.set_status(self.agent_type, 'paused')
        elif command['type'] == 'resume':
            self.state.set_status(self.agent_type, 'running')
        elif command['type'] == 'clear_history':
            self.state.clear_history_prefix(f'{self.agent_type}:')

    @abstractmethod
    async def work_loop(self):
        pass
