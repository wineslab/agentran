class ManagementAgent:
    def __init__(self, shared_state):
        self.state = shared_state

    def set_agent_intent(self, agent_type, intent):
        self.state.set_intent(agent_type, intent)

    def pause_agent(self, agent_type):
        self.state.set_status(agent_type, 'paused')

    def resume_agent(self, agent_type):
        self.state.set_status(agent_type, 'running')

    def clear_agent_history(self, agent_type):
        self.state.clear_history_prefix(f'{agent_type}:')

    def get_agent_status(self, agent_type):
        return {
            'status': self.state.get_status(agent_type),
            'intent': self.state.get_intent(agent_type),
        }
