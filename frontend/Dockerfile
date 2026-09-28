# Frontend Dockerfile

# Build stage
FROM node:20-alpine as builder

# Vite reads VITE_*-prefixed env vars at build time and inlines them into
# the bundle. Declare ARG + ENV so docker-compose build-args reach the npm
# script. Without this the bundle falls through to the runtime same-origin
# default in src/config.ts.
ARG VITE_API_URL
ENV VITE_API_URL=$VITE_API_URL

# v3.19 polished launch: when "true", `/` renders the public invite-only
# Landing page (`pages/Landing.tsx`) for anonymous visitors. Default
# stays "false" so existing dev deploys keep the historical
# `/` → `/dashboard` redirect. Authenticated users always go to
# /dashboard regardless of this flag.
ARG VITE_LANDING_PAGE_ENABLED=false
ENV VITE_LANDING_PAGE_ENABLED=$VITE_LANDING_PAGE_ENABLED

# PostHog product analytics (v3.24). VITE_POSTHOG_KEY is the project API
# key (phc_...) for the dedicated Meeting-Ops project; when empty,
# utils/posthog.ts init() is a no-op so dev/CI builds with no key carry
# zero analytics. VITE_POSTHOG_HOST defaults to the PostHog Cloud US
# ingestion endpoint (matches the rest of the UC ecosystem). Both are
# inlined at build time like every VITE_* var.
ARG VITE_POSTHOG_KEY
ENV VITE_POSTHOG_KEY=$VITE_POSTHOG_KEY
ARG VITE_POSTHOG_HOST=https://us.i.posthog.com
ENV VITE_POSTHOG_HOST=$VITE_POSTHOG_HOST

# Knowledge Graph page kill-switch (ship-dark). When "true" the
# /knowledge-graph nav item + page render; default "false" hides them (the
# backend endpoint is tier-gated regardless). Build with
# VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED=true to enable on a given deploy.
ARG VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED=false
ENV VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED=$VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED

# Rooms nav kill-switch for deploys with no physical mic (VPS). NO default
# on purpose: unset stays undefined in the bundle, and the runtime check in
# AppRouterSimplified treats anything but the literal "false" as ON — so
# the bigboy appliance build (which doesn't pass this arg) is unchanged.
# Cloud/VPS composes pass =false explicitly.
ARG VITE_ROOM_MODE_ENABLED
ENV VITE_ROOM_MODE_ENABLED=$VITE_ROOM_MODE_ENABLED

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY . .

# Build the application. build:docker now runs `tsc -b && vite build`
# (parity with `npm run build`), so a strict TypeScript error fails the
# Docker build instead of silently riding to production as it did before
# (17 strict errors + at least one real runtime bug in
# AdminBulkImport.tsx where showToast was called as a function instead
# of showToast.error(...) snuck through for weeks).
RUN npm run build:docker

# Production stage
FROM nginx:alpine

# Copy built files to nginx
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Expose port
EXPOSE 7777

# Start nginx
CMD ["nginx", "-g", "daemon off;"]