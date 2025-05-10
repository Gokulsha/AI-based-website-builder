import streamlit as st
import os
import tempfile
import base64
import docker
from PIL import Image
import io
import json
import shutil
import re
import atexit
import requests

# Initialize session state
if 'ai_provider' not in st.session_state:
    st.session_state.ai_provider = "openai"
if 'generated_code' not in st.session_state:
    st.session_state.generated_code = None
if 'docker_running' not in st.session_state:
    st.session_state.docker_running = False
if 'project_dir' not in st.session_state:
    st.session_state.project_dir = None
if 'docker_container' not in st.session_state:
    st.session_state.docker_container = None
if 'prefer_framework_config' not in st.session_state:
    st.session_state.prefer_framework_config = True  # Default to using our known-good config

# Constants
DEFAULT_DOCKERFILE = """
FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
"""

REACT_DOCKERFILE = """
# Stage 1: Build React app
FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

# Stage 2: Serve app
FROM nginx:alpine
COPY --from=builder /app/build /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
"""

STREAMLIT_DOCKERFILE = """
FROM python:3.9-slim
WORKDIR /app
COPY . .
RUN pip install streamlit
EXPOSE 8501
CMD ["streamlit", "run", "app.py"]
"""

SHINY_DOCKERFILE = """
FROM rocker/shiny:latest
WORKDIR /app
COPY . .
RUN R -e "install.packages('shiny')"
EXPOSE 3838
CMD ["R", "-e", "shiny::runApp('/app', host='0.0.0.0', port=3838)"]
"""

FRAMEWORK_CONFIGS = {
    "React": {
        "package_json": """{
  "name": "ai-generated-app",
  "version": "1.0.0",
  "private": true,
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "react-scripts": "5.0.1",
    "web-vitals": "^2.1.4"
  },
  "scripts": {
    "start": "react-scripts start",
    "build": "react-scripts build",
    "test": "react-scripts test",
    "eject": "react-scripts eject"
  },
  "eslintConfig": {
    "extends": ["react-app", "react-app/jest"]
  },
  "browserslist": {
    "production": [">0.2%", "not dead", "not op_mini all"],
    "development": ["last 1 chrome version", "last 1 firefox version", "last 1 safari version"]
  }
}""",
        "essential_files": {
            'public/index.html': """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>React App</title>
</head>
<body>
    <div id="root"></div>
</body>
</html>""",
            'src/index.js': """import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './App';

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);"""
        }
    },
    "Streamlit": {
        "requirements": """streamlit
pandas
numpy""",
        "essential_files": {
            'app.py': """import streamlit as st

def main():
    st.set_page_config(page_title="AI Generated App", layout="wide")
    st.title("My Streamlit App")
    
    with st.sidebar:
        st.header("Settings")
        name = st.text_input("Enter your name")
    
    if name:
        st.success(f"Hello, {name}!")
    else:
        st.info("Please enter your name in the sidebar")

if __name__ == "__main__":
    main()"""
        }
    },
    "Shiny": {
        "requirements": """shiny""",
        "essential_files": {
            'app.R': """library(shiny)

ui <- fluidPage(
    titlePanel("Shiny App"),
    sidebarLayout(
        sidebarPanel(
            textInput("name", "Enter your name")
        ),
        mainPanel(
            textOutput("greeting")
        )
    )
)

server <- function(input, output) {
    output$greeting <- renderText({
        paste("Hello,", input$name)
    })
}

shinyApp(ui = ui, server = server)"""
        }
    }
}

OPENAI_API_KEY = "xxxxxxxxxxxxxxxxxxxxxxxx"
DEEPSEEK_API_KEY = "xxxxxxxxxxxxxxxxxxxxxxxx" 
OPENAI_API_URL = "https://api.openai.com/v1"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1"

def get_language_from_filename(filename):
    ext = os.path.splitext(filename)[1].lower()
    return {
        '.html': 'html', '.css': 'css', '.js': 'javascript',
        '.jsx': 'jsx', '.json': 'json', '.ts': 'typescript',
        '.tsx': 'tsx', '.py': 'python', '.r': 'r',
        '.yaml': 'yaml', '.yml': 'yaml'
    }.get(ext, 'text')

def extract_code_blocks(content):
    code_blocks = re.findall(r'```(\w+)?\n(.*?)```', content, re.DOTALL)
    return {
        f"file{idx}.{ext if ext else 'txt'}": code.strip()
        for idx, (ext, code) in enumerate(code_blocks, 1)
    }

def call_ai_api(prompt, image_base64=None, tech_stack="HTML/CSS/JS"):
    try:
        if st.session_state.ai_provider == "openai":
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            }
            
            if image_base64:
                payload = {
                    "model": "gpt-4o",
                    "messages": [
                        {
                            "role": "system",
                            "content": f"You are an expert {tech_stack} developer who converts images to functional code"
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{image_base64}",
                                        "detail": "auto"
                                    }
                                }
                            ]
                        }
                    ],
                    "max_tokens": 4096
                }
            else:
                payload = {
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": f"You are an expert {tech_stack} developer"},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 4096
                }
            
            response = requests.post(
                f"{OPENAI_API_URL}/chat/completions",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            return response.json()
        
        elif st.session_state.ai_provider == "deepseek":
            headers = {
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": f"You are an expert {tech_stack} developer"},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 4096
            }
            
            response = requests.post(
                f"{DEEPSEEK_API_URL}/chat/completions",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            return response.json()
    
    except requests.exceptions.HTTPError as err:
        st.error(f"API Error: {err.response.text}")
        return None
    except Exception as e:
        st.error(f"API call failed: {str(e)}")
        return None

def process_image_input(image_file, tech_stack):
    try:
        if st.session_state.ai_provider != "openai":
            st.error("Image processing is only available with OpenAI")
            return
            
        img = Image.open(image_file)
        if img.mode != 'RGB':
            img = img.convert('RGB')
            
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=90)
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        
        prompt = f"""
        Generate complete {tech_stack} code for a website based on this image.
        Return all files in a structured JSON format with:
        - Keys as filenames (including proper extensions)
        - Values as file content
        Include ALL necessary files for the project.
        """
        
        response = call_ai_api(prompt, img_str, tech_stack)
        if response:
            process_ai_response(response, tech_stack)
    except Exception as e:
        st.error(f"Error processing image: {str(e)}")

def process_text_input(text_prompt, tech_stack):
    try:
        prompt = f"""
        Generate complete {tech_stack} code for a website with these features: {text_prompt}.
        Return all files in a structured JSON format with:
        - Keys as filenames (including proper extensions)
        - Values as file content
        Include ALL necessary files for the project.
        """
        
        response = call_ai_api(prompt, None, tech_stack)
        if response:
            process_ai_response(response, tech_stack)
    except Exception as e:
        st.error(f"Error processing text input: {str(e)}")

def process_ai_response(response, tech_stack):
    try:
        if not response or 'choices' not in response or not response['choices']:
            st.error("Invalid API response format")
            return
            
        content = response['choices'][0]['message']['content']
        
        try:
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0].strip()
            elif '```' in content:
                content = content.split('```')[1].split('```')[0].strip()
            
            code_data = json.loads(content)
        except json.JSONDecodeError:
            code_data = extract_code_blocks(content)
        
        st.session_state.generated_code = code_data
        project_dir = create_project_files(code_data, tech_stack)
        st.session_state.project_dir = project_dir
        
        docker_url = dockerize_project(project_dir, tech_stack)
        
        st.success("Website generated successfully!")
        st.markdown(f"### [View Live Preview]({docker_url})", unsafe_allow_html=True)
        
        for filename, code in code_data.items():
            with st.expander(f"View {filename}"):
                st.code(code, language=get_language_from_filename(filename))
    
    except Exception as e:
        st.error(f"Error processing AI response: {str(e)}")
        st.json(response)

def create_project_files(code_data, tech_stack):
    try:
        temp_dir = tempfile.mkdtemp()
        
        # Create framework-specific directories
        if tech_stack == "React":
            dirs = ['public', 'src/components', 'src/assets']
        elif tech_stack == "Streamlit":
            dirs = []
        elif tech_stack == "Shiny":
            dirs = []
        else:
            dirs = []
            
        for dir_path in dirs:
            os.makedirs(os.path.join(temp_dir, dir_path), exist_ok=True)

        # Process all files from AI response first (except package.json if we're using framework config)
        for filename, code in code_data.items():
            if tech_stack == "React" and filename == "package.json" and st.session_state.prefer_framework_config:
                continue  # Skip AI's package.json if we prefer our framework config
                
            if isinstance(code, dict):
                code = json.dumps(code, indent=2)
            
            if '/' in filename:
                dir_path = os.path.dirname(filename)
                os.makedirs(os.path.join(temp_dir, dir_path), exist_ok=True)
            
            full_path = os.path.join(temp_dir, filename)
            with open(full_path, 'w') as f:
                f.write(code)

        # Add essential framework files
        if tech_stack in FRAMEWORK_CONFIGS:
            config = FRAMEWORK_CONFIGS[tech_stack]
            for file_path, content in config['essential_files'].items():
                full_path = os.path.join(temp_dir, file_path)
                if not os.path.exists(full_path):
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    with open(full_path, 'w') as f:
                        f.write(content)
            
            # Always use our package.json for React if preferred
            if tech_stack == "React" and st.session_state.prefer_framework_config:
                pkg_path = os.path.join(temp_dir, 'package.json')
                with open(pkg_path, 'w') as f:
                    f.write(config['package_json'])
            
            # Add requirements file for Python frameworks
            if tech_stack in ["Streamlit"]:
                req_file = os.path.join(temp_dir, 'requirements.txt')
                if not os.path.exists(req_file):
                    with open(req_file, 'w') as f:
                        f.write(config['requirements'])

        return temp_dir
    except Exception as e:
        st.error(f"Error creating project files: {str(e)}")
        raise

def dockerize_project(project_dir, tech_stack):
    try:
        dockerfile_path = os.path.join(project_dir, "Dockerfile")
        
        dockerfile_content = {
            "React": REACT_DOCKERFILE,
            "Streamlit": STREAMLIT_DOCKERFILE,
            "Shiny": SHINY_DOCKERFILE
        }.get(tech_stack, DEFAULT_DOCKERFILE)
        
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content.strip())

        client = docker.from_env()
        image, build_logs = client.images.build(
            path=project_dir, 
            tag="web-builder",
            rm=True,
            forcerm=True
        )
        
        # Determine the port based on framework
        port_mapping = {
            "React": 80,
            "Streamlit": 8501,
            "Shiny": 3838
        }
        
        container = client.containers.run(
            image.id,
            ports={f'{port_mapping.get(tech_stack, 80)}/tcp': ('127.0.0.1', 0)},
            detach=True
        )
        container.reload()
        port = container.attrs['NetworkSettings']['Ports'][f'{port_mapping.get(tech_stack, 80)}/tcp'][0]['HostPort']
        
        st.session_state.docker_running = True
        st.session_state.docker_container = container
        
        return f"http://localhost:{port}"
    except Exception as e:
        st.error(f"Error dockerizing project: {str(e)}")
        raise
def cleanup():
    try:
        if st.session_state.get('docker_container'):
            container = st.session_state.docker_container
            container.stop()
            container.remove()
        
        if st.session_state.get('project_dir'):
            shutil.rmtree(st.session_state.project_dir)
    except Exception as e:
        print(f"Cleanup error: {str(e)}")
def main():
    st.set_page_config(page_title="AI Website Builder", layout="wide")
    st.title("AI-Powered Website Builder")
    
    with st.sidebar:
        st.subheader("AI Provider Settings")
        st.session_state.ai_provider = st.radio(
            "Select AI Provider:",
            ["openai", "deepseek"],
            index=0 if st.session_state.ai_provider == "openai" else 1,
            format_func=lambda x: "OpenAI GPT-4" if x == "openai" else "DeepSeek"
        )   
        if st.session_state.ai_provider == "openai":
            st.info("Supports both text and image inputs")
        else:
            st.info("Supports text inputs only")  
        st.subheader("React Configuration")
        st.session_state.prefer_framework_config = st.checkbox(
            "Use optimized React configuration",
            value=st.session_state.prefer_framework_config,
            help="When enabled, uses our pre-configured React setup instead of AI-generated package.json"
        )
    col1, col2 = st.columns([1, 1])
    with col1:
        input_method = st.radio(
            "Choose input method:", 
            ["Upload Image", "Text Prompt"],
            disabled=st.session_state.ai_provider == "deepseek"
        ) 
        tech_stack = st.selectbox(
            "Select tech stack:",
            ["HTML/CSS/JS", "React", "Streamlit", "Shiny"]
        )
    with col2:
        if input_method == "Upload Image":
            uploaded_file = st.file_uploader(
                "Upload website screenshot/sketch", 
                type=["png", "jpg", "jpeg"],
                disabled=st.session_state.ai_provider == "deepseek"
            )
            if uploaded_file:
                st.image(uploaded_file, use_container_width=True)
        else:
            text_prompt = st.text_area(
                "Describe your website:", 
                height=150,
                placeholder="A modern e-commerce site with product grid, search bar, and dark theme..."
            )    
    if st.button("Generate Website", type="primary"):
        with st.spinner("Generating website code..."):
            if input_method == "Upload Image" and uploaded_file is not None:
                process_image_input(uploaded_file, tech_stack)
            elif input_method == "Text Prompt" and text_prompt:
                process_text_input(text_prompt, tech_stack)
            else:
                st.warning("Please provide valid input")
    
    if st.session_state.generated_code and st.session_state.project_dir:
        with st.expander("Download Generated Code"):
            shutil.make_archive(st.session_state.project_dir, 'zip', st.session_state.project_dir)
            with open(f"{st.session_state.project_dir}.zip", "rb") as f:
                st.download_button(
                    label="Download Project as ZIP",
                    data=f,
                    file_name="generated_website.zip",
                    mime="application/zip"
                )

atexit.register(cleanup)

if __name__ == "__main__":
    main()