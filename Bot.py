import requests
import json
from groq import Groq
import os
from SimpleAgent import SimpleAgent
from bs4 import BeautifulSoup
import re
from datetime import datetime
from flask import Flask, request, redirect
import telebot
import threading
import time
import ast

# Your Spotify app credentials
client_id = os.getenv('SPOTIFY_CLIENT_ID')
client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')

def get_ngrok_url():
    response = requests.get("http://127.0.0.1:4040/api/tunnels")
    data = response.json()
    public_url = data['tunnels'][0]['public_url']
    return public_url

redirect_uri = f'{get_ngrok_url()}/callback'
print(redirect_uri)

groq_api_key = os.getenv('GROQ_API_KEY')
TELEGRAM_API_KEY = os.getenv('TELEGRAM_API_KEY')

app = Flask(__name__)
user_data = None
access_token = None
user_prompt = None
telegram_chat_id = None

def load_user_data(filename='user_data.json'):
    try:
        with open(filename, 'r') as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}  # Return an empty dictionary if the file does not exist or is corrupt

def save_user_data(data, filename='user_data.json'):
    with open(filename, 'w') as file:
        json.dump(data, file, indent=4)


# Telegram Bot Integration
bot = telebot.TeleBot(TELEGRAM_API_KEY)

def trim_string(input_string, in_char="{", out_char="}"):
    start_index = input_string.find(in_char)
    end_index = input_string.rfind(out_char)

    # Check if both '{' and '}' are found in the string
    if start_index != -1 and end_index != -1 and start_index < end_index:
        trimmed_string = input_string[start_index:end_index + 1]
    else:
        trimmed_string = ""  # Return an empty string if not found or in wrong order

    return trimmed_string
	
	
class SearchAgent(SimpleAgent):
    def __init__(self, api_key):
        system_prompt = (
            """You are a search agent. Your task is to generate search queries based on user prompts.

You will receive a JSON object with two fields:

'today': today's date, just in case the user asks something time-sensitive (in that case, you should use this information in your query)
'prompt': the original user prompt
'prior_results': list of prior results, if any. If this is an empty list, then this will be the first query composed based on this prompt. If there have been queries before, this list will contain one object per prior query, with the following properties:
    'query': the search query that was used in this step
    'search_results': summary of the results that were gathered
    'enough_information': boolean, are the results enough to answer the prompt fully?
    'go_deeper': boolean, should the next search go deeper into this topic (if true) or change strategies and search for something different (if false)
    'suggestions': some advice about how to continue the search process from this step

When the 'prior_results' list is not empty, use those prior results to inform your new query. If there are several objects in this list, it's likely that the last one will be related to what you need to do next, you can use the previous ones for context. Never use the exact same query twice.

You need to ONLY output the search query.
NEVER talk to the user. Do NOT engage with the user in any way, do NOT output anything else. The user cannot read your responses.
You are part of an automated Python pipeline. Your response will be used to perform a search and return results. This is why it's crucial that it's formatted correctly as a simple query.
Don't output the word 'query' or anything like that. Output what you want to search for directly.

Prioritize brevity and simplicity, this should look like a simple google search, you should write what any person would google in this situation, simple and brief."""
        )
        super().__init__(api_key=api_key, system=system_prompt, save_history = False)

    def run(self, prompt, prior_results):
        # Get today's date
        today = datetime.today().strftime("%A, %d %B %Y")
        input_prompt = json.dumps({'today': today, 'prompt': prompt, 'prior_results': json.dumps(prior_results, indent=4)}, indent=4)

        # Generate the search query using the provided prompt
        search_query = super().run(input_prompt, model = "llama3-70b-8192").strip()
        search_results = self.perform_search(search_query)
        
        return search_results, search_query

    def perform_search(self, query, max_items=10):
        query = query.replace(" ", "+")
    
        # Send a GET request to DuckDuckGo
        url = f"https://html.duckduckgo.com/html/?q={query}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        }

        try:
            response = requests.get(url, headers=headers)
        except:
            print(f"Failed to retrieve data: {response.status_code}")
            return []
            
        # Check if the request was successful
        if response.status_code != 200:
            print(f"Failed to retrieve data: {response.status_code}")
            return []
    
        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(response.content, "html.parser")
    
        # Extract search result titles and snippets
        results = []
        counter = 0
    
        # Update to select non-ad results
        for result in soup.find_all('div', class_='web-result'):
            title_tag = result.find('a', class_='result__a')
            snippet_tag = result.find('a', class_='result__snippet')
            
            # Extract and clean title
            title = re.sub(r'<.*?>', '', title_tag.text) if title_tag else None
            
            # Extract and clean snippet
            snippet = snippet_tag.text.strip() if snippet_tag else None
            
            # Extract URL
            url = title_tag.get('href') if title_tag else None
            
            if title and snippet:
                results.append({"id": counter, "title": title, "snippet": snippet, "url": url})
                counter += 1
            
            if counter >= max_items:
                break
    
        # Convert results list to JSON
        return json.dumps(results, indent=4)
		
class ScrapeAgent(SimpleAgent):
    def __init__(self, api_key):
        system_prompt = (
            """You are a scraping agent.
Your job is to analyze search results based on a user prompt and decide which results are likely to contain useful information to answer this prompt.
The search results are provided as a JSON object. Each item is a search result and contains the following keys:

'id' (the index of the result)
'title' (the title of the webpage)
'snippet' (a brief excerpt from the webpage)

Your task is to return a list of ids. These will be the pages that will be scraped.
This list should be formatted as a Python list, for example: [1, 4, 5] or [0].
If the required information is already present in the snippets, we do not need to scrape any of the webpages. In that case, output an empty list.
You must ALWAYS return a list, and the elements of the list can ONLY be integers.
NEVER talk to the user. Do NOT engage with the user in any way, do NOT output anything else. The user cannot read your responses.
You are part of an automated Python pipeline. Your response will be used to process these search results automatically. This is why it's crucial that it's formatted correctly as a Python list.

With that in mind, remember to choose all the results that would likely be relevant to answer the user's prompt."""
        )
        super().__init__(api_key=api_key, system=system_prompt, save_history = False)

    def scrape_webpage(self, url):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        }

        try:
            response = requests.get(url, headers=headers)
        except:
            print(f"Failed to retrieve webpage.")
            return ""
            
        if response.status_code != 200:
            print(f"Failed to retrieve webpage: {response.status_code}")
            return ""
        
        soup = BeautifulSoup(response.content, "html.parser")
        
        # Extract the main content of the page
        # This is highly dependent on the structure of the webpage.
        paragraphs = soup.find_all('p')
        full_text = "\n".join([p.get_text() for p in paragraphs])
        
        return full_text

    def run(self, prompt, search_results):
        # Prepare the formatted input for the model
        try:
            no_url_search_results = json.loads(search_results)
        except:
            print(f"Invalid search results: {search_results}")
            return "", False
        
        for item in no_url_search_results:
            if 'url' in item:
                del item['url']

        no_url_search_results = json.dumps(no_url_search_results, indent=4)
        
        input_prompt = (
            f"User Prompt: {prompt}\n\n"
            f"Search Results:\n{no_url_search_results}\n\n"
            "Based on the above information, output a list of the result indices that would likely help in answering the user's prompt, or an empty list if you think the answer is already present in the snippets. ONLY OUTPUT A LIST, AND MAKE IT A VALID LIST IN PYTHON, OR EVERYTHING WILL BREAK."
        )
        
        # Call the model to choose the correct index
        index_response = super().run(input_prompt, model="llama3-8b-8192")

        index_response = trim_string(index_response, in_char="[", out_char="]")

        # Parse and return the chosen index
        try:
            indices = json.loads(index_response.strip())
        except ValueError:
            print(f"Invalid index returned: {index_response}. Defaulting to [].")
            indices = []

        if len(indices) == 0:
            return no_url_search_results, False

        # Initialize the result list
        results = []
        search_results = json.loads(search_results)
    
        # Iterate through the list of indices
        for index in indices:
            # Find the search result that matches the current index
            selected_result = next((res for res in search_results if res['id'] == index), None)
    
            # If no matching result is found, continue to the next index
            if selected_result is None:
                continue
    
            # Get the URL and title from the selected result
            url = selected_result.get("url")
            title = selected_result.get("title")
    
            # If no URL is present, continue to the next index
            if not url:
                continue
    
            # Scrape the webpage content
            body = self.scrape_webpage(url)
    
            # Append the result object to the results list
            results.append({
                "index": index,
                "title": title,
                "body": body
            })
    
        # Convert the results list to a JSON formatted string
        return json.dumps(results, indent=4), True
		
		
class SummarizerAgent(SimpleAgent):
    def __init__(self, api_key):
        system_prompt = """You are a summarizer agent, part of a greater system which is trying to answer a user's prompt.
A web search has been performed and you will be given a particular search result. This will be in JSON format, with exactly two fields: 'title' and 'body'.
Your job is to output a JSON object with two fields:

'is_relevant': boolean value, this should be False if the search result is not at all relevant to answer the user's query, and should be True if the search result could be useful to answer the user's query (prompt).
'summary': string, summary of the information contained in the search result. This should include ALL useful information this result contains in relation to the prompt. You should not skip any details that are related to the prompt, but you should also not include anything irrelevant. If the search result is not relevant (marked as False in the 'is_relevant' property) this field should be an empty string.

You must ALWAYS return a valid JSON object with these two properties, and they must ALWAYS contain the right data types.
NEVER talk to the user. Do NOT engage with the user in any way, do NOT output anything else. The user cannot read your responses.
You are part of an automated Python pipeline. Your response will be used to process these search results automatically. This is why it's crucial that it's formatted correctly as a JSON object."""
        
        super().__init__(api_key=api_key, system=system_prompt, save_history = False)

    def run(self, prompt, webpage):
        input_prompt = (
            f"User Prompt: {prompt}\n\n"
            f"Search Result:\n{webpage}\n\n"
            "Based on the above information, output ONLY a VALID JSON object with the fields 'is_relevant' and 'summary'. Anything that you do NOT include in your summary, cannot be used to answer the user's prompt, so if this webpage is relevant, you should extract ALL useful information, be verbose. Of course, if it isn't relevant, remember to output an empty string in the 'summary' field."
        )

        response = super().run(input_prompt, model="llama3-8b-8192")
        response = trim_string(response).replace('True', 'true').replace('False', 'false')
        
        try:
            response = json.loads(response)
            if response['is_relevant']:
                return response['summary']
            else:
                return None
        except:
            print(f"Invalid JSON format returned by the summarizer: {response}")
            return None
			
class DeciderAgent(SimpleAgent):
    def __init__(self, api_key):
        system_prompt = """You are a decider agent, part of a greater system which is trying to answer a user's prompt.
A web search has been performed based on that prompt. You will be presented with the results of this search.
Your job is to decide if there is enough information in these results to answer the user's prompt.
Your output should be a JSON object with the following fields:

-enough_information: boolean value, True if there's enough information in these results to answer the user's prompt as they are (in which case no more search queries will be performed), False otherwise (more search queries are needed, either to go deeper into the concepts included in the search results or to search for something else).
-go_deeper: boolean value, True if there is some useful information in these results, but more search queries are needed to craft a comprehensive response, False if there is no useful information in these results, so subsequent search queries should switch gears.
-suggestions: string, advice to be passed on to the search agent than will write the next search query. Focus on what would be necessary to gather useful information, which could involve going deeper into one topic or changing strategies. Focus on one thing only, do not advice to search for many things at once, this may confuse the agent. Be as brief and relevant as possible.

NEVER talk to the user. Do NOT engage with the user in any way, do NOT output anything else. The user cannot read your responses.
You are part of an automated Python pipeline. Your response will be used to process these search results automatically. This is why it's crucial that it's formatted correctly as a valid JSON object."""
        
        super().__init__(api_key=api_key, system=system_prompt, save_history = False)

    def run(self, prompt, results):
        input_prompt = (
            f"User Prompt: {prompt}\n\n"
            f"Search Results:\n{results}\n\n"
            "Based on the above information, decide whether the search results are enough to answer the user's prompt and output a valid JSON object with the properties 'enough_information', 'go_deeper' and 'suggestions'."
        )
        
        result = super().run(input_prompt, model="llama3-70b-8192")
        result = trim_string(result).replace("True", "true").replace("False", "false")
        
        try:
            dict_result = json.loads(result)
            enough_information = dict_result['enough_information']
            go_deeper = dict_result['go_deeper']
            suggestions = dict_result['suggestions']

            return enough_information, go_deeper, suggestions
        except:
            print("DeciderAgent returned an incorrect response.")
            return False, False, ''
			
			
class FirstResponseAgent(SimpleAgent):
    def __init__(self, api_key):
        system_prompt = """You are part of a greater system tasked with answering a user's prompt.
The system has decided that this prompt required one or more web search queries.
Your job is to tell the user you will search for this information and get back to them soon.
You should speak directly to the user. The user does not know anything about the greater system behind the chatbot they're interacting with, so if you refer to it (for example, saying 'this sistem will...') the user will get confused. NEVER DO THIS.
Just let the user know you'll be right back with the answer. Please, always speak in the language of the user's prompt, that is very important. If the prompt is in spanish, use spanish. If the prompt is in english, use english. Whatever the language of the prompt, use that language.
        """
        super().__init__(system=system_prompt, api_key=api_key, save_history = False)

    def run(self, prompt):
        response = super().run(prompt, model="llama3-8b-8192")
        return response
		

class CompositeSearchAgent(SimpleAgent):
    def __init__(self, api_key):
        system_prompt = """You are part of a greater system tasked with answering a user's prompt.
The system has decided that this prompt required one or more web search queries.
These queries have been performed. You will receive them as a list of objects, each with the following properties:

-query: the search query that was used
-results: summary of the information that was found
-enough_information: whether this information was judged to be sufficient to answer the user's prompt

There might be only one object in this list (only one query was performed) or there may be more.
Your task is to take this information and compose a final response for the user.
Do not refer to the list or the objects explicitly, you should write directly to the user. The user cannot access or see this information in any way, so if you refer to it (for example, saying 'as you can see in the first item...') the user will get confused. NEVER DO THIS.
Inform yourself by the search results and craft a final response. Be as specific as you want, but be reasonable. The user probably does not need to know EVERYTHING that we have found in the search results, but comprehensive answers can be good in some situations. Use your best judgement to decide how verbose your response should be in relation to the original prompt.        
        """
        super().__init__(system=system_prompt, api_key=api_key)

        self.first_response_agent = FirstResponseAgent(api_key)
        self.search_agent = SearchAgent(api_key)
        self.scrape_agent = ScrapeAgent(api_key)
        self.summarizer_agent = SummarizerAgent(api_key)
        self.decider_agent = DeciderAgent(api_key)

    def run(self, prompt, bot = None, telegram_chat_id = None, max_iters = 3, verbose = False):
        first_response = self.first_response_agent.run(prompt)
        if bot is not None and telegram_chat_id is not None: bot.send_message(telegram_chat_id, first_response)
        if verbose: print(first_response)
        
        prior_results = []
        
        for iter in range(max_iters):
            search_results, search_query = self.search_agent.run(prompt, prior_results)
            if verbose: print("Search Query:", search_query)
            if verbose: print("Search Results:", search_results)
                
            results, scraped = self.scrape_agent.run(prompt, search_results)
            if verbose: print("Scraped Results:", scraped, results)
            
            all_summaries = []
            
            if scraped:
                for result in json.loads(results):
                    webpage = {'title': result['title'], 'body': result['body']}
                    summary = self.summarizer_agent.run(prompt, webpage)
                    all_summaries.append(summary)
            else:
                webpage = {'title': "NO WEBPAGE WAS SCRAPED, PRESENTING SEARCH RESULTS WITH SNIPPETS (SUMMARIZE ANYWAY!)", 'body': results}
                summary = self.summarizer_agent.run(prompt, webpage)
                all_summaries.append(summary)
            
            string_summaries = json.dumps(all_summaries, indent=4)
            if verbose: print("Summaries:", string_summaries)
            
            enough_information, go_deeper, suggestions = self.decider_agent.run(prompt, string_summaries)
            if verbose: print("Decider output:", enough_information, go_deeper, suggestions)

            prior_results.append({'query': search_query, 'results': string_summaries, 'enough_information': enough_information, 'go_deeper': go_deeper, 'suggestions': suggestions})

            if enough_information and not go_deeper:
                break

        if verbose: print(json.dumps(prior_results, indent=4))

        all_results = json.dumps(prior_results, indent=4)

        input_prompt = (
            f"User Prompt: {prompt}\n\n"
            f"Search Results:\n{all_results}\n\n"
            "This is really important: ALWAYS ANSWER THE USER IN THE LANGUAGE THEY USED IN THE PROMPT!"
        )
        
        response = super().run(input_prompt, model="llama3-8b-8192")
        
        if bot is not None and telegram_chat_id is not None: bot.send_message(telegram_chat_id, response)
        if verbose: print(response)
        
        return response, first_response
		
		
def post_auth_process(telegram_chat_id, recent_auth = False):
    
    
    system = f"""You are a helpful assistant, speaking to a user who is asking us to create a Spotify playlist from a specific request.
    {'The user has been asked to authorize the Spotify login, and this has been successful.' if recent_auth else ''}
    Please inform the user that {'the authorization was successful and that ' if recent_auth else ''}their playlist is being processed, but might take a few seconds.
    Use the same language as the user's prompt to answer. Be brief."""

    agent = SimpleAgent(groq_api_key, system)
    prompt = user_data[telegram_chat_id]['user_prompt']
    response = agent.run(prompt)
    
    # Notify the user that authorization was successful
    bot.send_message(telegram_chat_id, response)

    # Create the playlist based on the user's prompt
    user_prompt = user_data[telegram_chat_id]['user_prompt']
    
    try:
        playlist_url = playlist_from_prompt(user_prompt, telegram_chat_id, verbose=True)


        system = """You are a helpful assistant, speaking to a user who is asking us to create a Spotify playlist from a specific request.
        The user has sent us the request and the playlist has been successfully created.
        Your job is to inform the user they can now listen to the playlist in the provided URL. You need to provide them with the URL, since they don't have it.
        Use the same language as the user's prompt to answer. Be brief."""

        agent = SimpleAgent(groq_api_key, system)
        prompt = f"ORIGINAL USER PROMPT: {user_data[telegram_chat_id]['user_prompt']}\nPLAYLIST URL:{playlist_url}" 
        response = agent.run(prompt)

        # Notify the user that authorization was successful
        bot.send_message(telegram_chat_id, response)
    except Exception as e:
        print(f"Post auth process has failed with the following error: {e}")
        system = """You are a helpful assistant, speaking to a user who is asking us to create a Spotify playlist from a specific request.
        The user has sent us the request, but we had an issue with the system and the playlist could not be generated.
        Your job is to inform the user of this error, apologize and ask them to try again or to wait a few minutes.
        Use the same language as the user's prompt to answer. Be brief."""

        agent = SimpleAgent(groq_api_key, system)
        prompt = f"ORIGINAL USER PROMPT: {user_data[telegram_chat_id]['user_prompt']}" 
        response = agent.run(prompt)

        # Notify the user that authorization was successful
        bot.send_message(telegram_chat_id, response)

        
# Step 1: Home route to redirect to Spotify authorization
@app.route('/')
def home():
    telegram_chat_id = request.args.get('chat_id')
    auth_url = (
        'https://accounts.spotify.com/authorize?'
        f'client_id={client_id}&response_type=code&redirect_uri={redirect_uri}&scope=playlist-modify-private'
        f'&state={telegram_chat_id}'  # Include state parameter
    )
    return redirect(auth_url)


# Step 2: Callback route to handle Spotify's redirect and exchange code for token
@app.route('/callback')
def callback():
    code = request.args.get('code')
    state = request.args.get('state')  # Get state parameter to identify the user
    telegram_chat_id = int(state)
    
    token_url = 'https://accounts.spotify.com/api/token'
    response = requests.post(token_url, data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'client_secret': client_secret
    })
    response_data = response.json()
    
    # Save access token and refresh token in user data
    user_data[telegram_chat_id]['access_token'] = response_data['access_token']
    user_data[telegram_chat_id]['refresh_token'] = response_data['refresh_token']
    user_data[telegram_chat_id]['token_expiration'] = time.time() + response_data['expires_in']
    
    save_user_data(user_data)
    
    post_auth_process(telegram_chat_id, recent_auth = True)
    
    return "Playlist created successfully. You can now close this window."

def refresh_access_token(telegram_chat_id):
    refresh_token = user_data[telegram_chat_id]['refresh_token']
    token_url = 'https://accounts.spotify.com/api/token'
    response = requests.post(token_url, data={
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
        'client_secret': client_secret
    })
    response_data = response.json()
    
    # Update access token and expiration time
    user_data[telegram_chat_id]['access_token'] = response_data['access_token']
    user_data[telegram_chat_id]['token_expiration'] = time.time() + response_data['expires_in']
    
    save_user_data(user_data)

def get_access_token(telegram_chat_id):
    if time.time() > user_data[telegram_chat_id]['token_expiration']:
        refresh_access_token(telegram_chat_id)
    return user_data[telegram_chat_id]['access_token']


def get_user_id(access_token):
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    user_profile_url = 'https://api.spotify.com/v1/me'
    user_response = requests.get(user_profile_url, headers=headers)
    return user_response.json()['id']

def create_playlist(name, description, access_token):
    user_id = get_user_id(access_token)
    create_playlist_url = f'https://api.spotify.com/v1/users/{user_id}/playlists'
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    playlist_data = {
        "name": name,
        "description": description,
        "public": False
    }
    response = requests.post(create_playlist_url, headers=headers, json=playlist_data)
    return response.json()

def search_tracks(query, access_token, track_type='track'):
    search_url = 'https://api.spotify.com/v1/search'
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    params = {
        'q': query,
        'type': track_type
    }
    search_response = requests.get(search_url, headers=headers, params=params)
    return search_response.json()['tracks']['items']

def add_tracks_to_playlist(playlist_id, track_uris, access_token):
    add_tracks_url = f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks'
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    response = requests.post(add_tracks_url, headers=headers, json={"uris": track_uris})
    return response.status_code


def simple_track_json(tracks, max_tracks = 5):
    new_json_result = []

    n_tracks = min(len(tracks), max_tracks)
    
    i = 0
    
    while len(new_json_result) < n_tracks and i < len(tracks):
        track = tracks[i]
        
        if track['type'] != 'track':
            i+=1
            continue
        
        this_json = {}

        this_json["index"] = i
        this_json["song_name"] = track["name"]
        this_json["artist"] = track["artists"][0]["name"]
        this_json["album_name"] = track["album"]["name"]
        this_json["uri"] = track["uri"]

        new_json_result.append(this_json)
        
        i+=1

    new_json_result = json.dumps(new_json_result)
    
    return new_json_result

def playlist_from_prompt(user_prompt, telegram_chat_id, verbose=False):
    access_token = get_access_token(telegram_chat_id)
    
    print(f'Starting playlist creation process! We will process this request: "{user_prompt}"')
    
    if not verbose:
        print("Please wait, this can take a few minutes.")
    
    # create the playlist with a name and a description
    
    system = """The user will ask you to create a spotify playlist about something. Your job is to come up with a name and description for this playlist.
    You should output this in JSON format, as an object with the name and description properties. Do not output ANYTHING ELSE, you should ONLY output the valid JSON object with these two properties. This is important because your output will be used in a structured python script.
    You are not talking to the user, do not address the user in any way, do not come up with specific songs or include anything else in the JSON, just these two properties."""
    agent = SimpleAgent(groq_api_key, system)
    response = agent.run(user_prompt)
    response = json.loads(response)
    playlist = create_playlist(response["name"], response["description"], access_token)
    
    if verbose:
        print(f'A playlist has been initialized under the name "{response["name"]}" and the description "{response["description"]}"')
        print()
    
    # propose a list of song/artist pairs
    
    system = """The user will ask you to create a spotify playlist about something.
    Your job is to make a list of song/artist pairs that will satisfy the user according to their request.
    Your output should be a list of song/artist pairs, in JSON format (a list of elements with song and artist properties).
    Create a playlist of 20 songs, unless the user specifies something different. Do not output ANYTHING ELSE, you should ONLY output the valid JSON object. This is important because your output will be used in a structured python script.
    You are not talking to the user, do not address the user in any way, just output the JSON."""
    agent = SimpleAgent(groq_api_key, system)
    response = agent.run(user_prompt)
    song_artist_pairs = json.loads(response)
    
    if verbose:
        print(f"A list of songs has been proposed for your playlist, here is the first version:\n{response}")
        print()
    
    # search for each song on spotify and pick the best tracks
    
    selected_tracks = []

    for song in song_artist_pairs:
        search_string = song["song"] + " - " + song["artist"]
        tracks = search_tracks(search_string, access_token)

        tracks = simple_track_json(tracks)

        system = """You will be given a requested song and a JSON object of possible tracks found on Spotify by searching for the requested song. Your job is to pick one of those tracks as the correct song.
        If there are several songs that could be correct, choose the one that you think is most relevant.
        Pick studio versions over live versions, and originals over remixes, unless specified otherwise.
        Your pick should be the track that most people would think about when prompted with this specific song request.
        
        Each possible track will have an index. Your output should be only this integer, or -1 if no tracks are even remotely close to the desired track.
        
        Do not output ANYTHING ELSE, you should ONLY output a single integer. This is important because your output will be used in a structured python script. You are not talking to the user, do not address the user in any way, just output the integer index of the chosen track."""

        agent = SimpleAgent(groq_api_key, system)
        prompt = "REQUESTED SONG: \"" + search_string + "\"\n\nFound tracks:\n\n" + tracks
        index = int(agent.run(prompt, model="llama3-8b-8192"))
        tracks = json.loads(tracks)

        if index != -1:
            selected = tracks[index]
            selected_tracks.append(selected)
    
    for i in range(len(selected_tracks)):
        selected_tracks[i]['index'] = i
    
    if verbose:
        print(f"Each song has been searched for on Spotify and specific tracks have been selected as follows:\n{selected_tracks}")
        print()
    
    # review the playlist and exclude irrelevant songs
    
    system = f"""You are part of an agentic system tasked with creating a spotify playlist based on a user's request.
    The user has issued a request, and another agent in this system has already created a playlist. Your job is to check that every song in this playlist is relevant and correct given the user's request, and that no songs are repeated. Your output should be a list of numbers, corresponding to the indices of the songs that you think should be excluded from the playlist. If all songs are relevant and correct, output an empty list.

    The list should be formatted as a Python list, so, for example: [2, 9] would indicate that the songs at indices 2 and 9 should be excluded, and [] would indicate that all songs should be included.

    Do not output ANYTHING ELSE, you should ONLY output the valid list. This is important because your output will be used in a structured python script. You are not talking to the user, do not address the user in any way, just output the list.

    We want as many songs as possible from this proposed playlist to be included in the final playlist. Therefore, only exclude very obvious mistakes. If there are no obvious mistakes, please output an empty list."""

    agent = SimpleAgent(groq_api_key, system)
    
    prompt = f"""USER REQUEST: {user_prompt}
    
    PROPOSED PLAYLIST:
    
    {json.dumps(selected_tracks)}"""
    
    to_exclude = agent.run(prompt)
    to_exclude = ast.literal_eval(to_exclude)
    
    if verbose:
        print(f"The following songs have been found irrelevant and will be excluded from the playlist:\n{to_exclude}")
        print()
    
    filtered_tracks = []

    for track in selected_tracks:
        if track['index'] not in to_exclude:
            filtered_tracks.append(track)
    
    if verbose:
        print(f"Here is your final playlist:\n{filtered_tracks}")
        print()
            
    # add tracks to the playlist        
    
    playlist_id = playlist["id"]
    track_uris = [track["uri"] for track in filtered_tracks]

    add_tracks_to_playlist(playlist_id, track_uris, access_token)

    print(f"The playlist has been successfully created, you can listen to it here:")
    print(f"{playlist['external_urls']['spotify']}")
    print("Enjoy!")
    
    return playlist['external_urls']['spotify']
	



@bot.message_handler(func=lambda message: True)
def handle_message(message):
    telegram_chat_id = message.chat.id

    if "/playlist" in message.text:
        pass
        user_prompt = message.text.split("/playlist")[-1].strip()
        
        if telegram_chat_id not in user_data:
            user_data[telegram_chat_id] = {
                'user_prompt': user_prompt
            }
        else:
            user_data[telegram_chat_id]['user_prompt'] = user_prompt
            
        save_user_data(user_data)
        
        # Check if user is already authorized
        if 'access_token' in user_data[telegram_chat_id]:
            post_auth_process(telegram_chat_id, recent_auth = False)
            
        else:  
            system = """You are a helpful assistant, speaking to a user who is asking us to create a Spotify playlist from a specific request.
            The user needs to authorize us to make changes to their Spotify account to create the playlist and add songs.
            Please inform the user that they should click on the provided link to give this authorization in order to continue with the process. You need to provide them with the URL, since they don't have it yet.
            Use the same language as the user's prompt to answer. Be brief."""

            agent = SimpleAgent(groq_api_key, system)

            # Example usage
            ngrok_url = get_ngrok_url() + f"/?chat_id={telegram_chat_id}"
            
            prompt = f"""USER PROMPT: {user_prompt}\nSPOTIFY AUTH URL: {ngrok_url}"""
            response = agent.run(prompt)

            bot.send_message(telegram_chat_id, response)
    elif "/search" in message.text:
        user_prompt = message.text.split("/search")[-1].strip()
        agent = CompositeSearchAgent(groq_api_key)
        response, first_response = agent.run(user_prompt, bot, telegram_chat_id, verbose=True)
    elif "/help" in message.text:
        bot.send_message(telegram_chat_id, "Send '/playlist' command to start creating (CURRENTLY OFFLINE), or /search to find information about stuff!")
    
    else:
        system = f"""You are a helpful assistant who can engage in conversation with a user, as a part of an AI system who tries to help with a user's request.
        Please instruct the user to use the '/playlist' command, followed by a prompt, to create a playlist automatically, or the '/search' command, followed by a prompt, to search the web. Be nice and funny. Answer in the same language as the user's message. Be brief."""

        agent = SimpleAgent(groq_api_key, system)

        prompt = message.text

        response = agent.run(prompt)
        
        bot.send_message(telegram_chat_id, response)


def start_telegram_bot():
    bot.polling()

if __name__ == '__main__':
    user_data = load_user_data()
    threading.Thread(target=start_telegram_bot).start()
    app.run(host='127.0.0.1', port=3000)  # Ensure this matches the port in your Glitch redirect URI

    # Wait for the access_token to be set
    while access_token is None:
        time.sleep(1)