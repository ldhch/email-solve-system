# Stage 1: build the React SPA (Vite + TypeScript).
FROM node:20-alpine AS build

WORKDIR /build
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

# Stage 2: serve static assets with Nginx and proxy /api to the backend.
FROM nginx:1.27-alpine
COPY --from=build /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
RUN mkdir -p /var/www/certbot

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
