# Privacy and API keys

Watson is built for materials that shouldn't leave your computer without your say-so — unpublished manuscripts, participant data, embargoed findings. Here's exactly what happens to your documents and your key, and when.

## Your documents

Everything you upload stays in a project folder on your own computer. The only time any document text leaves your machine is when you explicitly click **Start processing** — at that point, the text of the documents in that run is sent to Google's Gemini API so the model can read them. Nothing is sent on project creation, on page load, in the background, or as part of navigating around the app.

Your results, reports, and source documents are never uploaded anywhere by Watson itself. If you want to share or back them up, that's a manual step you take (for example, downloading the project as a zip).

## Your API key

Watson needs a Gemini API key to run checks, since the model itself is Google's, not something bundled into the app. When you save a key in **Settings**, Watson stores it in your operating system's native secure credential store:

- **macOS:** Keychain
- **Windows:** Credential Manager
- **Linux:** Secret Service

Watson talks to these stores directly — it doesn't go through a configurable backend that could be redirected to a plaintext file. The key is never written into a project file, and it's excluded from any exports or downloaded archives.

Watson never reads the credential store on its own — not at launch, on page load, or after an update. It reads the saved key only after you start a run that needs it. After that, the key lives only in the app's memory for as long as Watson is running; closing Watson clears it. If an update changes the installed app identity, your operating system may ask you to approve that first run's Keychain access.

## What this means day to day

- Adding files, browsing results, renaming a project — none of it touches the network.
- The only outbound call Watson makes is the one you start on purpose, and only for the documents in that run.
- If you never save an API key, Watson simply can't run a check — there is no credential-store access until you start one.
