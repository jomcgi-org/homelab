# Trips Web App

Interactive trip viewer for exploring travel routes and photos.

## Features

- **Photo Timeline** - Scroll through photos with playback controls
- **Route Visualization** - See routes on an interactive map (MapLibre)
- **Real-time Sync** - WebSocket updates as new photos are added
- **Mobile-friendly** - Responsive design with touch navigation

## Tech Stack

- React 19
- Vite for bundling
- MapLibre GL for maps
- TailwindCSS for styling

## Development

```bash
# Install dependencies
npm install

# Start dev server
npm run dev
```

The app connects to the trips API (`api.jomcgi.dev/trips`) for photo and route data.

## Deployment

Deployed to Cloudflare Pages via Bazel `wrangler_pages` in BuildBuddy CI. The `push_all_pages` target runs on every push to main.

## Related

- `projects/trips/backend/` - Backend API serving photo data
- `projects/trips/tools/publish-trip-images/` - CLI for uploading trip photos
