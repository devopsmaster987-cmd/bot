
# Random Browser Session — Selenium Learning Project

A Python and Selenium project for learning browser navigation, internal-link discovery, scrolling, timed sessions, action counting, and graceful shutdown.

**Note:** Automated advertising clicks are disabled. Use repeated browser automation only on websites you own or have permission to test.

## Features

- Opens a visible Google Chrome browser.
- Discovers internal website links.
- Navigates to randomly selected internal pages.
- Scrolls pages in different directions.
- Runs repeated sessions lasting 60–120 seconds.
- Counts successful navigation actions.
- Pauses for one hour after 1,000 navigation actions.
- Uses fallback URLs when no internal links are found.
- Stops safely with Ctrl+C.
- Closes the browser when the program exits.

## Requirements

- Python 3.8 or newer.
- Google Chrome.
- Internet connection.
- Selenium Python package.

## Project Structure

    random-browser-session/
    ├── browse.py
    ├── requirements.txt
    └── README.md

## Installation on Windows

Open PowerShell in the project folder.

Check Python:

    python --version

Install the dependencies:

    python -m pip install -r requirements.txt

## Run the Application

Execute:

    python browse.py

Chrome should open and begin navigating the configured website.

Press Ctrl+C in PowerShell to stop the application.

## Configuration

Edit the settings near the top of browse.py.

- BASE_URL: Website to test.
- ALLOWED_HOSTS: Permitted website hostnames.
- MIN_SESSION_SECONDS: Minimum session duration.
- MAX_SESSION_SECONDS: Maximum session duration.
- ACTION_LIMIT: Navigation actions before a pause.
- WAIT_AFTER_LIMIT_SECONDS: Pause duration in seconds.
- FALLBACK_PAGES: Backup URLs used when no links are found.

The default pause is 3,600 seconds, which equals one hour.

## How It Works

### 1. Browser Initialization

Selenium starts Chrome using:

    driver = webdriver.Chrome(options=options)

### 2. Link Discovery

The script searches for anchor elements and reads their href attributes.

It checks that each URL belongs to an approved hostname.

### 3. Page Navigation

The script randomly chooses an internal URL and navigates to it.

### 4. Scrolling

JavaScript scrolls the page by varying distances. The script occasionally scrolls upward.

### 5. Action Counting

Successful navigation actions increment the counter. This counter does not represent every mouse click.

### 6. Pause

When the counter reaches 1,000, the program pauses for one hour and then resets the counter.

### 7. Shutdown

Press Ctrl+C to stop. The program attempts to close Chrome cleanly.

## Troubleshooting

### Python Is Not Recognized

Install Python and enable its PATH option. Reopen PowerShell afterward.

### Chrome Does Not Open

Update Selenium:

    python -m pip install --upgrade selenium

Make sure Google Chrome is installed.

### No Internal Links Are Found

Check the website's rendered HTML and verify the fallback URLs.

### Navigation Fails

Check your internet connection, the URL, and the website's availability.

### Some Pages Time Out

Slow websites or pages with long-running network requests may exceed the configured page-load timeout.

## Learning Exercises

- Save visited URLs to a log file.
- Capture screenshots after navigation.
- Add explicit waits for test elements.
- Add a maximum number of sessions.
- Write unit tests for URL validation.
- Record page titles and navigation errors.

## Responsible Use

- Test only websites you own or have permission to test.
- Do not automate third-party advertising clicks.
- Avoid excessive requests to live production websites.
- Respect website terms and rate limits.

## License

This project is provided for educational use. Add an appropriate license if you intend to distribute it.
