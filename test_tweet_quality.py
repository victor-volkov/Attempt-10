from app import evaluate_tweet_quality

# Test cases
tweets = [
    {
        "type": "High quality tweet",
        "text": "Just discovered a fascinating paper on quantum computing's potential impact on cryptography. The way quantum algorithms could potentially break current encryption methods while enabling new, quantum-resistant protocols is mind-blowing. We need to start preparing for the post-quantum era now. #tech #cybersecurity"
    },
    {
        "type": "Spam/Advertisement",
        "text": "Check out my new post!!! Click here to win FREE BITCOIN!!! 🚀🚀🚀 #crypto #btc #makemoney"
    },
    {
        "type": "Normal quality tweet",
        "text": "Bitcoin just reached a new all-time high. Interesting to see how the market reacts in the coming days. #BTC"
    }
]

print("Testing tweet quality evaluation:")
print("-" * 50)

for tweet in tweets:
    print(f"\nTesting {tweet['type']}:")
    print(f"Tweet: {tweet['text']}")
    result = evaluate_tweet_quality(tweet['text'])
    print(f"Score: {result['score']}")
    print(f"Reason: {result['reason']}")
    print("-" * 50) 