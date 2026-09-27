FROM node:22-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN groupadd --gid 10001 dispatcher \
    && useradd --uid 10001 --gid dispatcher --no-create-home --shell /usr/sbin/nologin dispatcher \
    && mkdir -p /app/node_modules/.vite-temp
ENV HOME=/tmp
USER 10001:10001
EXPOSE 5173
CMD ["npm", "run", "dev"]
