FROM node:18-bullseye-slim

WORKDIR /app

ENV NODE_OPTIONS=--openssl-legacy-provider

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    make \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json ./
RUN npm ci --production=false

COPY . .

RUN cd frontend && npm install --production=false && npm run build

ENTRYPOINT ["node", "server.js"]
