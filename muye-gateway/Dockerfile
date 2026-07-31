FROM node:22-alpine AS web-build
WORKDIR /web
COPY web/package.json ./
RUN npm install --ignore-scripts
COPY web ./
RUN npm run build

FROM nginx:1.27-alpine
COPY muye-gateway/nginx/conf.d/muye-gateway.conf.template /etc/nginx/templates/default.conf.template
COPY --from=web-build /web/dist /usr/share/nginx/html
