from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/submit_temperature', methods=['POST'])
def submit_temperature():
    data = request.get_json()
    temp = data.get('temperature')
    
    try:
        # Convert to float and validate
        temperature = float(temp)
        
        # Server-side validation: check if within 40-200 range
        if temperature < 40 or temperature > 200:
            print(f"ERROR: Temperature {temperature}°F is outside acceptable range (40-200°F)")
            return jsonify({'status': 'error', 'message': f'Temperature {temperature}°F is outside acceptable range (40-200°F)'}), 400
        
        # Print to console
        print(f"Temperature submitted: {temperature}°F")
        
        return jsonify({'status': 'success', 'message': f'Temperature {temperature}°F received successfully'})
    
    except (ValueError, TypeError):
        print(f"ERROR: Invalid temperature value received: {temp}")
        return jsonify({'status': 'error', 'message': 'Invalid temperature value'}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

