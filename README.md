# Twitter Search & Comment Generator

A web application that allows you to search for tweets, generate AI-powered responses using GPT-4, and post comments using Twitter API.

## Features

- Search tweets using various criteria (keywords, minimum likes, replies, retweets)
- View tweet details including author information and engagement metrics
- Generate contextual responses using GPT-4
- Post comments directly to Twitter
- Customizable AI persona for comment generation

## Setup

1. Clone the repository
2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file based on `.env.example` and fill in your API keys:
- APIFY_API_TOKEN (from apify.com)
- OPENAI_API_KEY (from platform.openai.com)
- Twitter API credentials (from developer.twitter.com)

4. Customize the AI persona (optional):
- Edit `config/persona.txt` to modify how GPT-4 generates comments

## Usage

1. Start the application:
```bash
python app.py
```

2. Open your browser and navigate to `http://localhost:5000`

3. Enter your search criteria and click "Search Tweets"

4. For each tweet in the results:
   - Click "Generate Comment" to create an AI-powered response
   - Review the generated comment
   - Click "Post Comment" to publish it as a reply on Twitter

## Directory Structure

```
├── app.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── .env                  # API keys and configuration
├── config/
│   └── persona.txt       # GPT-4 persona configuration
├── templates/
│   └── index.html        # Web interface
└── twitter/
    ├── twitter_client.py # Twitter API v2 client
    └── oauth2.py         # OAuth2 authentication helper
```

## Security Notes

- Never commit your `.env` file
- Keep your API keys secure
- Review generated comments before posting

## Customization

You can customize the AI persona by editing `config/persona.txt`. This file contains instructions that guide GPT-4 in generating comments. Modify it to match your desired tone and style. 