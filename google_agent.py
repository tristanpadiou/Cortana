from __future__ import annotations
from composio_langgraph import Action, ComposioToolSet, App
from composio_tools_agent import Composio_agent

from pydantic_graph import BaseNode, End, GraphRunContext, Graph
from pydantic_ai import Agent
from datetime import datetime
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langchain.output_parsers import RetryOutputParser
from pydantic import BaseModel,Field
from dataclasses import dataclass
from typing_extensions import TypedDict
from typing import Annotated, List, Optional

import os
from dotenv import load_dotenv 

#get graph visuals
from IPython.display import Image, display
import os
import requests
import nest_asyncio
nest_asyncio.apply()
load_dotenv()
GOOGLE_API_KEY=os.getenv('google_api_key')
pse=os.getenv('pse')




@dataclass
class State:
    node_messages:list
    evaluator_message:str
    query: str
    plan: List
    node_query_template:dict
    node_query: str
    route:str
    n_retries:int

class Google_agent:
    def __init__(self,llms: dict, api_keys:dict):
        """
        Args:
            llm (any): The language model to use using langchain_framework
            api_keys (dict): The API keys to use
        """
        # tools is the composio toolset
        self.tools=ComposioToolSet(api_key=api_keys['composio_key'])
        # tool_shemas is a dictionary of the tool names and the actions they can perform
        self.tool_shemas={
            'Mail Manager':{tool.name:tool for tool in self.tools.get_action_schemas(apps=[App.GMAIL])},
            'Maps Manager':{tool.name:tool for tool in self.tools.get_action_schemas(apps=[App.GOOGLE_MAPS])},
            'Tasks Manager':{tool.name:tool for tool in self.tools.get_action_schemas(apps=[App.GOOGLETASKS])},
            'Google images tool':{'search_images':'search for images'},
            'Get current time':{'get_current_time':'get the current time'},
            'list_tools':{'list_tools':'list the tools available'}
        }
        # tool_functions is a dictionary of the tool names and the actions they can perform
        self.tool_functions={
            'Mail Manager':{tool.name:tool.description for tool in self.tools.get_tools(apps=[App.GMAIL])},
            'Maps Manager':{tool.name:tool.description for tool in self.tools.get_tools(apps=[App.GOOGLE_MAPS])},
            'Tasks Manager':{tool.name:tool.description for tool in self.tools.get_tools(apps=[App.GOOGLETASKS])},
            'Google images tool':{'search_images':'search for images'},
            'Get current time':{'get_current_time':'get the current time'},
            'list_tools':{'list_tools':'list the tools available'}
        }
        # agents are the composio agents for the tools
        self.mail_agent=Composio_agent(self.tools.get_tools(apps=[App.GMAIL]),llms['openai_llm'])
        self.maps_agent=Composio_agent(self.tools.get_tools(apps=[App.GOOGLE_MAPS]),llms['openai_llm'])
        self.tasks_agent=Composio_agent(self.tools.get_tools(apps=[App.GOOGLETASKS]),llms['openai_llm'])
        
    

        # Nodes:
        # planner_node is the node that generates the plan
        @dataclass
        class planner_node(BaseNode[State]):
            llm=llms['pydantic_llm']
            tool_functions=self.tool_functions
            async def run(self,ctx: GraphRunContext[State])->agent_node | End:
                class task_shema(BaseModel):
                    task: str = Field(description='description of the task')
                    manager_tool: str = Field(description= 'the name of the manager tool to use')
                    action: str = Field(description=' the action that the manager tool must take')
                class plan_shema(BaseModel):
                    tasks: List[task_shema] = Field(description='the list of tasks that the agent need to complete to succesfully complete the query')
                

                
                plan_agent=Agent(self.llm,output_type=plan_shema, instructions=f'based on this query: {ctx.state.query} generate a plan using those manager tools: {self.tool_functions} to get the necessary info and to complete the query, the plan cannot contain more than 10 tasks')
                try:
                    response=plan_agent.run_sync(ctx.state.query) 
                    ctx.state.plan=response.output.tasks
                    return agent_node()
                          
                except:
                    return End(ctx.state)
                

        # agent_node is the node that uses the plan to complete the task and update the node_query if needed
        @dataclass
        class agent_node(BaseNode[State]):
            llm=llms['pydantic_llm']
            tool_shemas=self.tool_shemas
            async def run(self,ctx: GraphRunContext[State])-> get_current_time_node | maps_manager_node | tasks_manager_node | mail_manager_node | google_image_search_node | list_tools_node | End:
                
                class task_route(BaseModel):
                    query: str = Field(description='the query to be passed to one of the manager tool nodes')

                class query_template(BaseModel):
                    query_template: str = Field(description='the query template')
                    query: str = Field(description='the query based on the query template and the previous node message to be passed to the manager tool')
                    
                plan= ctx.state.plan
                if plan:
                    # check if the query is already in the node_query for the manager tool and the action for the agent to reuse or update
                    
                    if ctx.state.evaluator_message:
                        agent=Agent(self.llm, output_type=query_template, instructions=f'based on the evaluator message, update the query template, and generate a new query based on the task, the tool_shemas and the previous query_template and the previous node messages')
                        response=agent.run_sync(f'task:{plan[0]}, evaluator_message:{ctx.state.evaluator_message}, previous_node_messages:{ctx.state.node_messages[-1]}, previous_query_template:{ctx.state.node_query_template.get(plan[0].manager_tool).get(plan[0].action).get('query')}, tool_shemas:{self.tool_shemas[plan[0].manager_tool][plan[0].action]}') 
                        # check if the action is already in the node_query for the manager tool
                        if ctx.state.node_query_template[plan[0].manager_tool].get(plan[0].action):
                            ctx.state.node_query_template[plan[0].manager_tool][plan[0].action]={'query':response.output.query_template}
                        # if the action is not in the node_query for the manager tool, add it
                        else:
                            ctx.state.node_query_template[plan[0].manager_tool]={plan[0].action:{'query':response.output.query_template}}
                        
                    else:
                        if ctx.state.node_query_template:
                            try:
                                if ctx.state.node_query_template[plan[0].manager_tool].get(plan[0].action).get('query'):
                                    # if the action is already in the node_query for the manager tool, update the query template
                                    agent=Agent(self.llm, output_type=task_route, instructions=f'based on a task, generate a query to be passed to the manager_tool mentionned in the task, use the informations from previous nodes, base the query on the given query_template')
                                    response=agent.run_sync(f'task:{plan[0]},query_template:{ctx.state.node_query_template[plan[0].manager_tool].get(plan[0].action).get('query')}, previous_node_messages:{ctx.state.node_messages[-1]}, tool_shemas:{self.tool_shemas[plan[0].manager_tool][plan[0].action]}') 
                                    
                            except:
                                agent=Agent(self.llm, output_type=query_template, instructions=f'based on a task, generate a query to be passed to the manager_tool mentionned in the task and a query template for the action for future use, use the informations from previous nodes, tailor the query to the shema of the tool')
                                response=agent.run_sync(f'task:{plan[0]}, previous_node_messages:{ctx.state.node_messages}, tool_shemas:{self.tool_shemas[plan[0].manager_tool][plan[0].action]}') 
                                ctx.state.node_query_template[plan[0].manager_tool]={plan[0].action:{'query':response.output.query_template}}
                            

                        else:
                            agent=Agent(self.llm, output_type=query_template, instructions=f'based on a task, generate a query to be passed to the manager_tool mentionned in the task and a query template for the action for future use, use the informations from previous nodes, tailor the query to the shema of the tool')
                            response=agent.run_sync(f'task:{plan[0]}, previous_node_messages:{ctx.state.node_messages}, tool_shemas:{self.tool_shemas[plan[0].manager_tool][plan[0].action]}') 
                            ctx.state.node_query_template={plan[0].manager_tool:{plan[0].action:{'query':response.output.query_template}}}

                    ctx.state.node_query=response.output.query
                    ctx.state.route=plan[0].manager_tool
                    if ctx.state.route=='get_current_time':
                        return get_current_time_node()
                    elif ctx.state.route=='Maps Manager':
                        return maps_manager_node()
                    elif ctx.state.route=='Tasks Manager':
                        return tasks_manager_node()
                    elif ctx.state.route=='Mail Manager':
                        return mail_manager_node()
                    elif ctx.state.route=='Google images tool':
                        return google_image_search_node()
                    elif ctx.state.route=='list_tools':
                        return list_tools_node()
                    else:
                        return End(ctx.state)
                    
            
                else:
                    return End(ctx.state)
                    

        # evaluator_node is the node that evaluates the task and prompts the agent to retry or update the node_query if needed
        class evaluator_node(BaseNode[State]):
            llm=llms['pydantic_llm']
            async def run(self,ctx: GraphRunContext[State])-> agent_node | End:
            
                class Status(BaseModel):
                    status: str = Field(description='completed or failed')
                    reason: Optional[str] = Field(default_factory=None,description='the reason for the status, if the status is completed, the reason should be None')
                evaluator_agent=Agent(self.llm,output_type=Status, instructions=f'based on the node message and the prompt, decide if the task was completed or failed, if failed, explain why')

                
                response=evaluator_agent.run_sync(f'node_message:{ctx.state.node_messages[-1]}, node_query:{ctx.state.node_query}')
                    
                status=response.output.status          
                        
                # if the task is failed, prompt the agent to retry or update the node_query if needed
                if status =='failed':
                    ctx.state.plan=[]
                    ctx.state.evaluator_message=f'task: {ctx.state.node_query[ctx.state.route]}, failed, reason: {response.output.reason}'
                    ctx.state.n_retries+=1
                    if ctx.state.n_retries>2:
                        if len(ctx.state.node_messages)>10:
                            del ctx.state.node_messages[0]
                        return End(ctx.state)
                    else:
                        return agent_node()
                else:
                    
                    ctx.state.n_retries=0
                    ctx.state.evaluator_message=''
                    del ctx.state.plan[0]
                    if len(ctx.state.node_messages)>10:
                            del ctx.state.node_messages[0]
                    return agent_node()


        class google_image_search_node(BaseNode[State]):
            async def run(self,ctx: GraphRunContext[State])->evaluator_node:
                """Search for images using Google Custom Search API
                args: query
                return: image url
                """
                # Define the API endpoint for Google Custom Search
                url = "https://www.googleapis.com/customsearch/v1"
                query=ctx.state.node_query[ctx.state.route]['query']

                params = {
                    "q": query,
                    "cx": api_keys['pse'],
                    "key": api_keys['google_api_key'],
                    "searchType": "image",  # Search for images
                    "num": 1  # Number of results to fetch
                }

                # Make the request to the Google Custom Search API
                response = requests.get(url, params=params)
                data = response.json()

                # Check if the response contains image results
                if 'items' in data:
                    # Extract the first image result
                    image_url = data['items'][0]['link']
                    ctx.state.node_messages.append({'google_image_search':f'here is the url for the image {image_url}'})
                    return evaluator_node()
                else:
                    ctx.state.node_messages.append({'google_image_search':'failed to find image'})
                    return evaluator_node()


        @dataclass
        class tasks_manager_node(BaseNode[State]):
            """use this tool to answer task related queries
            this tool can:
            list tasks
            create tasks
            get task details
            complete a task (which also deletes it :) )

            args: query - pass the entire tasks related queries directly here
            
            """
            tasks_agent=self.tasks_agent
            async def run(self,ctx: GraphRunContext[State])->evaluator_node:

                response=self.tasks_agent.chat(ctx.state.node_query[ctx.state.route]['query'])
                # return response
                ctx.state.node_messages.append({'tasks_manager':response})
                return evaluator_node()


        @dataclass
        class maps_manager_node(BaseNode[State]):
            """tool to use to answer maps and location queries
            this tool can:
            find locations such as restorants, bowling alleys, museums and others
            display those locations's infos (eg. adress, name, url, price range)
            args: query - pass the maps or loc related queries directly here
            return: locations with urls
            """
            maps_agent=self.maps_agent
            async def run(self,ctx: GraphRunContext[State])->evaluator_node:
                response=self.maps_agent.chat(ctx.state.node_query[ctx.state.route]['query'])
                # return response
                ctx.state.node_messages.append({'maps_manager':response})
                return evaluator_node()



        @dataclass
        class mail_manager_node(BaseNode[State]):
            """Tool to use to answer any email related queries
            this tool can:
            show the inbox
            
            create an email
            create a draft email
            verify the email content
            send the email

            args: query - pass the email related queries directly here
            """
            mail_agent=self.mail_agent
            async def run(self,ctx: GraphRunContext[State])->evaluator_node:
                response=self.mail_agent.chat(ctx.state.node_query[ctx.state.route]['query'] + f'if the query is about sending an email, do not send any attachements, just send the url in the body')
                # return response
                ctx.state.node_messages.append({'mail_manager':response})
                return evaluator_node()



        
        class get_current_time_node(BaseNode[State]):
            """
            Use this tool to get the current time.
            Returns:
                str: The current time in a formatted string
            """
            async def run(self,ctx: GraphRunContext[State])->evaluator_node:
                ctx.state.node_messages.append({'get_current_time':f"The current time is {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"})
                return evaluator_node()
            
        @dataclass
        class list_tools_node(BaseNode[State]):
            tools=self.tool_functions
            async def run(self,ctx: GraphRunContext[State])->evaluator_node:
                ctx.state.node_messages.append({'list_tools':self.tools})
                return End(ctx.state)

        self.graph=Graph(nodes=[planner_node, agent_node, evaluator_node, google_image_search_node, tasks_manager_node, maps_manager_node, mail_manager_node, get_current_time_node, list_tools_node])
        self.state=State(node_messages=[], evaluator_message='', query='', plan=[], node_query_template={}, node_query='', route='', n_retries=0)
        self.planner_node=planner_node()
        
    def chat(self,query:str):
        """Chat with the google agent,
        Args:
            query (str): The query to search for
        Returns:
            str: The response from the google agent
        """
        self.state.query=query
        response=self.graph.run_sync(self.planner_node,state=self.state)
        return response.output

    def display_graph(self):
        """Display the graph of the google agent
        Returns:
            Image: The image of the graph
        """
        image=self.graph.mermaid_image()
        return display(Image(image))