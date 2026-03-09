# AI Ark MCP Server

An MCP (Model Context Protocol) server that gives AI agents full access to the [AI Ark](https://ai-ark.com) API — search 400M+ people, 69M+ companies, find verified emails, phone numbers, and analyze personalities.

Works with Cursor, Claude, Windsurf, and any MCP-compatible client.

**Hosted at:** `https://ai-ark-mcp.fly.dev/mcp`
**GitHub:** [github.com/dropoutsanta/ai-ark-mcp](https://github.com/dropoutsanta/ai-ark-mcp)

## Quick Setup

Add this to your MCP config (e.g. `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "ai-ark": {
      "url": "https://ai-ark-mcp.fly.dev/mcp"
    }
  }
}
```

When you connect, you'll be prompted to enter your AI Ark API key. Get one at [ai-ark.com → API → Developer Portal](https://ai-ark.com).

That's it. No server to run, no webhook setup, no config files.

## What You Can Do

### Search People (instant)

Find anyone by title, company, industry, location, skills, and more. Returns full profiles with LinkedIn, work history, education — everything except email.

```
"Find CTOs at software companies in New York with 50-200 employees"
```

### Search Companies (instant)

Search 69M+ companies by industry, size, tech stack, revenue, location, or find lookalikes.

```
"Find companies similar to Stripe with 50-200 employees"
```

### Find Verified Emails (async — 15-120 seconds)

This is the most powerful feature. Search for people and get their verified email addresses in one flow. Here's how it works:

```
Step 1: "Find emails for CEOs at SaaS companies in the US"
        → Agent calls export_people_with_email → gets a trackId

Step 2: Agent automatically polls get_export_results(trackId)
        → "processing... 2/5 emails found"
        → "processing... 4/5 emails found"
        → Returns full data with verified emails
```

Each email is verified in real time by BounceBan. You get the email address, validation status (VALID/INVALID), and the MX provider (Google, Microsoft, etc).

**How this works under the hood:** AI Ark's API delivers email results via webhook — but AI agents can't receive webhooks. Our MCP server handles this automatically. It hosts a webhook receiver on Fly.io, catches the results, stores them, and serves them back to the agent when it polls. You don't need to think about any of this — just ask for emails and they show up.

### Find Phone Numbers (instant)

Look up mobile numbers by LinkedIn URL or by company domain + name.

```
"Find the phone number for John Doe at acme.com"
"Get the mobile number for linkedin.com/in/janedoe"
```

### Reverse Lookup (instant)

Have an email or phone number? Find out who it belongs to — returns the full profile.

```
"Who is john@example.com?"
"Look up +14155551234"
```

### Personality Analysis (instant)

Analyze anyone's personality from their LinkedIn profile. Returns DISC assessment, Big Five scores, communication style, and selling tips — great for personalizing outreach.

```
"Analyze the personality of linkedin.com/in/johndoe"
```

### Check Credits (instant)

```
"How many AI Ark credits do I have left?"
```

## All 10 Tools

| Tool | What it does | Speed |
|------|-------------|-------|
| `search_companies` | Search 69M+ companies by industry, size, tech, location, revenue | Instant |
| `search_people` | Search 400M+ people by title, skills, seniority, company filters | Instant |
| `export_people_with_email` | Search people + find their verified emails | Async (15-120s) |
| `find_emails_by_track_id` | Find emails for results from a previous search_people call | Async (15-120s) |
| `get_export_results` | Poll for email results (call after export or find_emails) | Instant |
| `get_email_statistics` | Quick progress check on an email-finding job | Instant |
| `reverse_people_lookup` | Look up a person by email or phone number | Instant |
| `find_mobile_phone` | Find phone numbers by LinkedIn URL or domain + name | Instant |
| `analyze_personality` | DISC/Big Five personality analysis from LinkedIn | Instant |
| `get_credits` | Check remaining API credits | Instant |

## The Email Webhook Problem (and how we solved it)

AI Ark's email-finding API is webhook-only — when emails are done, they POST results to a URL you provide. There's no endpoint to fetch results by ID.

This is a problem for AI agents and local tools that can't expose a public HTTP endpoint.

**Our solution:** This MCP server runs on Fly.io and acts as the webhook receiver. When you ask for emails:

1. The MCP server generates a unique callback URL on itself (`https://ai-ark-mcp.fly.dev/webhook/{id}`)
2. It passes that URL to AI Ark as the webhook
3. AI Ark verifies the emails and POSTs results back to our server
4. Results are stored on disk
5. When the agent polls `get_export_results`, it reads from the stored data

The agent just sees: ask for emails → poll → get emails. No webhook complexity.

## Self-Hosting

If you want to run your own instance:

```bash
git clone https://github.com/dropoutsanta/ai-ark-mcp.git
cd ai-ark-mcp

# Deploy to Fly.io
fly launch
fly volumes create ark_data --size 1 --region yyz
fly deploy

# Set your base URL
fly secrets set MCP_BASE_URL=https://your-app.fly.dev
```

Update your MCP config to point to your instance:

```json
{
  "mcpServers": {
    "ai-ark": {
      "url": "https://your-app.fly.dev/mcp"
    }
  }
}
```

**Note:** Email finding requires a deployed server (for webhook reception). If you run the MCP locally without a public URL, everything works except email-related tools.

## Authentication

Uses OAuth 2.1. When you first connect from Cursor/Claude, a browser window opens where you paste your AI Ark API key. After that, you stay authenticated — even across server restarts. Each MCP connection gets its own credentials, so you can have multiple API keys connected simultaneously.

## Credits

Built by [Nick Tomic](https://linkedin.com/in/nicktomic). API by [AI Ark](https://ai-ark.com).
