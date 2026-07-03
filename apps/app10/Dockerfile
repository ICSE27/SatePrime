FROM node:18-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libvips-dev \
    build-essential \
    python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm install --production --ignore-scripts \
    && npm install --production --save-exact sharp@0.33.5 @tensorflow/tfjs-node@4.22.0 \
    && npm cache clean --force

COPY *.js ./
COPY Images/ ./Images/
COPY Model/ ./Model/
COPY MobilenetModel/ ./MobilenetModel/

ENTRYPOINT ["node", "app.js"]
CMD ["--images_dir=Images", "--model_dir=Model", "--epochs=1"]
