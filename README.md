
# Dopamine

Dopamine is a feature-rich Discord moderation and utility bot built with a focus on performance and ease of use. Originally designed as a private project to express creativity and give back to the Discord community, it is now open-sourced under the AGPL-3.0 license.

The bot is designed to handle everything from point-based moderation to automated server engagement tools like Haiku detection and scheduled messages.

This a 100% free-to-use, non-profit (solo) Discord bot project by LikerOfTurtles.

## Features

### Moderation & Administration

* Point-Based System: A customizable moderation system where actions (warn, ban, etc.) carry point values. Includes an automated decay system so users aren't penalized indefinitely for old mistakes.

* Slowmode Scheduler: Schedule slowmode to enable/disable automatically at specific times of the day.

* Comprehensive Logging: Detailed logs for all moderator actions and bot activities.

### Utility & Engagement

* Scheduled Messages: Set up recurring announcements (daily, weekly, monthly) using a simple modal-based setup.

* Starboard & LFG: Community-driven features for highlighting great content or organizing groups for gaming/events.

* Haiku Detection: Automatically detects messages in the 5-7-5 syllable format and reformats them.

* Member Tracker: Tracks server growth with customizable embeds and goal-setting.

* Global Notes: Store and retrieve notes across any server where the bot is present (requires user-install/profile addition).

## Installation & Setup

### Prerequisites:

* Python 3.x

* A Discord Bot Token (via Discord Developer Portal)

* Dependencies listed in requirements.txt

### Local Setup:

1. Clone the repository:

```git clone https://github.com/yourusername/dopamine.git```

2. Install the required packages:

```pip install -r requirements.txt```

3. Configure your environment variables (Token, etc.) in a .env file.

4. Run the bot:

```python main.py```

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0). This means if you modify the bot and run it as a service, you must share your modified source code under the same license.

## Contributing

Since the project is now open source, contributions, bug reports, and pull requests are welcome. Please ensure that any additions follow the existing optimization patterns (asynchronous DB calls and connection pooling).
