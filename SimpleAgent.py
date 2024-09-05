import os
from groq import Groq

class SimpleAgent:
    def __init__(self, api_key, system="You are a helpful assistant", save_history=True):
        self.client = Groq(api_key=api_key)
        self.system = system
        self.save_history = save_history
        
        if self.save_history:
            self.messages = []
            self.messages.append({"role": "system", "content": self.system})

    def get_text_response(self, prompt, model="llama3-8b-8192"):
        if self.save_history:
            self.messages.append({"role": "user", "content": prompt})
            messages = self.messages
        else: 
            messages = [
                {"role": "system", "content": self.system},
                {"role": "user", "content": prompt}
            ]

        chat_completion = self.client.chat.completions.create(
            messages=messages,
            model=model,
        )
        
        response = chat_completion.choices[0].message.content
        
        if self.save_history:
            self.messages.append({"role": "assistant", "content": response})

        return response

    def run(self, prompt, model="llama3-8b-8192"):
        return self.get_text_response(prompt, model)
