from __future__ import annotations
import asyncio
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest,ModelResponse

from dataclasses import dataclass
from datetime import datetime
from pydantic import Field



@dataclass
class Message_state:
    messages: list[ModelMessage]

@dataclass
class Deps:
    agents_output: dict
    user:str
    
class Cortana_agent:
    def __init__(self,llm:any, tools:list = [], mpc_servers:list = [], summarizer:bool = False, custom_summarizer_agent:Agent = None, memory_length:int = 20, memory_summarizer_length:int = 15):
        """
        Args:
            llm (any): The LLM to use as a model has to be a pydantic_ai model
            \n
            example:
            GoogleModel('gemini-2.5-flash', provider=GoogleProvider(api_key=api_keys['google_api_key']))
            OpenAIModel('gpt-4.1-mini',provider=OpenAIProvider(api_key=api_keys['openai_api_key']))
            \n
            summarizer (bool): Whether to use the summarizer agent or not default is False
            \n
            custom_summarizer_agent (Agent): The custom summarizer agent to use, if not provided, the main agent will be used, it has to be a pydantic_ai agent
            \n
            memory_length (int): The number of messages to keep in memory before summarizing, default is 20
            \n
            memory_summarizer_length (int): The number of messages to summarize, default is 15
            \n
            mpc_servers (list): The list of MCP servers to use:
            \n
                example:
                [
                    MCPServerStreamableHTTP(url='https://mcp.notion.com/mcp', headers=None),
                    MCPServerSSE(url='https://mcp.notion.com/sse', headers=None),
                    MCPServerStdio(command='npx', args=['-y', 'mcp-remote', 'https://mcp.notion.com/mcp'], env=None)
                ]
            
            tools (list): The list of tools to use as functions:
              \n
              example:
              [
                tool_1,
                tool_2,
                tool_3
              ]
        """
        
        self.llm=llm
        self.tools=tools
        self.mpc_servers = mpc_servers
        
        
        #summarize old messages
        self.summarize=summarizer
        self.memory_length=memory_length
        self.memory_summarizer_length=memory_summarizer_length
        self.custom_summarizer_agent=custom_summarizer_agent
        if self.summarize:
            if not self.custom_summarizer_agent:
                self.summarize_agent=Agent(llm,instructions='Summarize this conversation, omitting small talk and unrelated topics. Focus on the technical discussion and next steps.')
            else:
                self.summarize_agent=self.custom_summarizer_agent

               
        
        self._mcp_context_manager = None
        self._is_connected = False
        
        #agent
        @dataclass
        class Cortana_output:
            ui_version: str= Field(description='a markdown format version of the answer for displays if necessary')
            voice_version: str = Field(description='a conversationnal version of the answer for text to voice')

        instructions = """
        # You are Cortana - A Helpful AI Assistant

        ## Your Role:
        You are Cortana, a helpful assistant capable of handling a wide range of tasks.

        ## Available Information:
        - Current time and date
        - User query and context
        - User's name (always refer to them by their first name)
        - Various tools and capabilities
        - always ask the user for confirmation before using any tool

        """
        
        self.agent=Agent(
            self.llm, 
            output_type=Cortana_output, 
            tools=self.tools,
            mcp_servers=self.mpc_servers, 
            instructions=instructions
        )
        self.memory=Message_state(messages=[])
        self.deps=Deps(agents_output={}, user='')
    
    async def connect(self):
        """Establish persistent connection to MCP server"""
        if not self._is_connected:
            self._mcp_context_manager = self.agent.run_mcp_servers()
            await self._mcp_context_manager.__aenter__()
            self._is_connected = True
            print("Connected to MCP server")

    async def disconnect(self):
        """Close the MCP server connection"""
        if self._is_connected and self._mcp_context_manager:
            try:
                await self._mcp_context_manager.__aexit__(None, None, None)
                print("Disconnected from MCP server")
            except RuntimeError as e:
                if "Attempted to exit cancel scope in a different task" in str(e):
                    # This is expected when disconnecting from a different task context
                    print("MCP server disconnected (task context changed)")
                else:
                    raise e
            except Exception as e:
                print(f"Error during MCP disconnect: {e}")
            finally:
                self._is_connected = False
                self._mcp_context_manager = None
    #summarize old messages
    async def summarizer(self,result):
        """
        function to summarize memory.messages when it is too long
        args:
            result: the models output
        
        """
        if len(result.all_messages()) > self.memory_length:
            oldest_messages=[]
            for i in result.all_messages()[:self.memory_summarizer_length]:
                
                if isinstance(i,ModelRequest):
                    if isinstance(i.parts[0].content,list):
                        oldest_messages.append({'user_query':i.parts[0].content[0]})
                    else:
                        oldest_messages.append({'user_query':i.parts[0].content})
                elif isinstance(i,ModelResponse):
                    oldest_messages.append({'model_response':i})
            summary = await self.summarize_agent.run(f'oldest messages: {str(oldest_messages)}')
            # Return the last message and the summary
            self.memory.messages=summary.new_messages() + result.new_messages()
        else:
            self.memory.messages=result.all_messages()
    async def chat(self, query:list):
        """
        # Chat Function Documentation

        This function enables interaction with the user through various types of input.

        ## Parameters

        - `query`: The input to process. A list of inputs of the following types:
        - String: Direct text input passed to the agent
        - Binary content: Special format for media files (see below)

        ## Binary Content Types

        The function supports different types of media through `BinaryContent` objects:

        ### Audio
        ```python
        cortana_agent.chat([
            'optional string message',
            BinaryContent(data=audio, media_type='audio/wav')
        ])
        ```

        ### PDF Files
        ```python
        cortana_agent.chat([
            'optional string message',
            BinaryContent(data=pdf_path.read_bytes(), media_type='application/pdf')
        ])
        ```

        ### Images
        ```python
        cortana_agent.chat([
            'optional string message',
            BinaryContent(data=image_response.content, media_type='image/png')
        ])
        ```

        ## Returns

        - `Cortana_output`: as a pydantic object, the ui_version and voice_version are the two fields of the object

        ## Extra Notes
        The deps and message_history of cortana can be accessed using the following code:
        ```python
        cortana_agent.deps
        cortana_agent.memory.messages
        ```
        """
        if not self._is_connected:
            await self.connect()
            
        result=await self.agent.run(query, deps=self.deps, message_history=self.memory.messages)
        
        # Start summarizer in background - don't wait for it
        if self.summarize:
            asyncio.create_task(self.summarizer(result))

        return result.output
    
    def reset(self):
        """
        Resets the Cortana agent to its initial state.

        Returns:
            str: A confirmation message indicating that the agent has been reset.
        """
        self.memory.messages=[]
        self.deps=Deps(agents_output={}, user='')
        return f'Cortana has been reset'
    
    async def __aenter__(self):
        """Async context manager entry"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.disconnect()
