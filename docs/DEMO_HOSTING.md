# Free demo hosting

Three free services, none of them on your laptop:

| Part | Where | Notes |
|---|---|---|
| Database | MongoDB Atlas (free M0 cluster) | 512 MB. |
| API and sourcing worker | Render (free web service) | One process: the worker runs inside the API (`RUN_WORKER_IN_API=1`). It sleeps after about 15 minutes without traffic and takes 30 to 60 seconds to wake. A sourcing run that was in progress resumes after it wakes. |
| Web app | Vercel (Hobby plan) | The Hobby plan is for non-commercial use. |

This is for a demo. Free tiers change; check each provider's current limits. For real use, move to one small paid server.

## 1. Make three secrets (keep them out of git)

```sh
.venv/bin/sourcer-admin generate-key      # SECRETS_KEY: encrypts stored GitHub tokens
openssl rand -base64 36                   # API_SERVICE_KEY: shared by the API and the web app
openssl rand -base64 36                   # SESSION_SECRET: signs the login cookie (web app only)
```

Use new values for the demo, not your local ones.

## 2. Database (MongoDB Atlas)

1. Create a free account and a free **M0** cluster.
2. Create a database user with a strong password.
3. Under network access, allow connections from anywhere (`0.0.0.0/0`). Render's free service has no fixed address. The user and password are what protect it.
4. Copy the connection string (`mongodb+srv://...`). It is your `MONGODB_URI`.

## 3. API (Render)

1. Push this repo to GitHub (done) and create a **Blueprint** on Render from it. `render.yaml` sets everything except the three secrets.
2. When asked, enter `MONGODB_URI`, `SECRETS_KEY` and `API_SERVICE_KEY`.
3. After the deploy, open `https://<your-service>.onrender.com/health`. It should answer `{"status":"ok"}`.

## 4. Create the company and the HR login

Run this from your laptop, pointing at the hosted database. The values are only in the shell, not in a file:

```sh
export MONGODB_URI='mongodb+srv://...'      # the Atlas string
export SECRETS_KEY='...'                    # the same one you gave Render
.venv/bin/sourcer-admin setup               # asks for the company name and the HR email; prints a one-time password
```

Send the one-time password to HR privately. They must change it at first sign-in. To let a company run sourcing, add its GitHub token with `sourcer-admin tenant set-github-token`.

## 5. Web app (Vercel)

1. Import `crewdesk-web` into Vercel.
2. Set these environment variables:

   | Name | Value |
   |---|---|
   | `API_URL` | `https://<your-service>.onrender.com` |
   | `API_SERVICE_KEY` | the same value you gave Render |
   | `SESSION_SECRET` | the third secret |
   | `TRUST_PROXY` | `1` |

3. Deploy. The address Vercel gives you is what HR opens.

## 6. Check it

- Open the web address and sign in as HR; change the password.
- Create an employee and a login link; open the link in a private window and set a password.
- The first request after a quiet period is slow while the API wakes up. A free uptime monitor that opens `/health` every 10 minutes keeps it awake, if you want that.

## Limits to remember

- Use dummy employees and no real candidate data on a demo.
- The free database has no automatic backup. Export anything you care about.
- If the API restarts during a sourcing run, the run continues from its last saved point.
