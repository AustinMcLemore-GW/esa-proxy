# Phase I ESA Proxy — Deployment Guide

## What this is
A lightweight Python server that queries all 16 ASTM E1527-21 required databases
on your behalf and returns the results to the browser tool. It solves the CORS
problem that prevents browsers from calling government APIs directly.

## Deploy to Render (free, ~10 minutes)

### Step 1 — Create a GitHub repository
1. Go to https://github.com and sign in (or create a free account)
2. Click the "+" icon → "New repository"
3. Name it: esa-proxy
4. Set to Public
5. Click "Create repository"
6. Upload these three files: app.py, requirements.txt, render.yaml

### Step 2 — Deploy on Render
1. Go to https://render.com and sign in with your GitHub account
2. Click "New +" → "Web Service"
3. Connect your GitHub account if prompted
4. Select the esa-proxy repository
5. Render will auto-detect the render.yaml — click "Create Web Service"
6. Wait ~3 minutes for the build to complete
7. Your proxy URL will be: https://esa-proxy.onrender.com
   (Render may append a random suffix — copy the exact URL shown)

### Step 3 — Update the browser tool
Paste your proxy URL into the tool when prompted. That's it.

## Your proxy URL
Once deployed, your queries look like this:
https://your-proxy-url.onrender.com/query?lat=27.852924&lon=-82.703508&zip=33615

## Notes
- Render free tier spins down after 15 minutes of inactivity
- First request after inactivity takes ~30 seconds to wake up
- All subsequent requests are fast
- To keep it always-on, upgrade to Render's $7/month Starter plan
