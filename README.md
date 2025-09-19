Telegram Multichannel Content Automation Bot
This project is a production-grade asynchronous Telegram bot for automating the management, generation, forwarding, and advertising of posts across multiple channels and social media platforms. The bot supports advanced scheduling, content transformation (including watermarking and translation), and seamless integration with Instagram Threads and X (Twitter).

Features

Multi-channel Telegram content forwarding and automation

Scheduled and recurring mass mailings with media support

AI-powered content generation for text and images (OpenAI, Gemini, Claude, Llama, etc.)

Watermark addition and external watermark removal options for images and tables

Built-in translation for posts and multilingual support

Integration for crossposting to Instagram Threads and X (Twitter)

Advanced statistics and scheduled reporting for posts, views, reactions, and subscribers

Flexible proxy support and admin interface

Installation

Clone this repository:

text
git clone <repository-url>
cd <repository-folder>
Install Python dependencies:

text
pip install -r requirements.txt
Dependencies include:

aiogram (Telegram Bot API)

pyrogram + tgcrypto

aiohttp, apscheduler

pillow, opencv-python

instagrapi, tweepy

requests, openai

Configure your environment variables and tokens in .env or configuration files, as detailed in the config.py module.

Run the bot:

text
python main.py
Usage

Set up your channels, bots, and (optionally) proxies in the configuration.

Use the admin Telegram interface to add tasks for forwarding, content generation, or advertising (details customizable).

Manage scheduled mailings and content generation directly from Telegram.

Monitor and export statistics through bot commands/file outputs.

Requirements

Python 3.9+

Telegram bot token and API credentials

(Optional) Instagram/X credentials for social media integration

(Optional) Proxy server access for geo-distributed use

Contributions

Pull requests, issues, and suggestions are welcome!
