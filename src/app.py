"""Module providing a entry point to application."""

from flask import Flask, request, Response, jsonify
import logging.config
import json
import requests

app = Flask(__name__)

with open('../codenow/config/log-config.json', 'rt') as f:
    config = json.load(f)

logging.config.dictConfig(config)

@app.route('/')
def root_route():
    """Function for serving route /."""
    headers = {
        'X-B3-TraceId': request.headers.get('X-B3-TraceId'),
        'X-B3-SpanId': request.headers.get('X-B3-SpanId'),
        'X-B3-ParentSpanId': request.headers.get('X-B3-ParentSpanId'),
        'X-B3-Sampled': request.headers.get('X-B3-Sampled'),
        'X-B3-Flags': request.headers.get('X-B3-Flags')
    }
    app.logger.info('INFO log message')
    result = "Hello World!"
    resp = Response(result)
    resp.headers['X-B3-TraceId'] = headers.get('X-B3-TraceId')
    resp.headers['X-B3-SpanId'] = headers.get('X-B3-SpanId')
    return resp

@app.route('/health')
def health_check():
    return jsonify(status='UP'), 200

if __name__ == '__main__':
    app.run(port=8080)