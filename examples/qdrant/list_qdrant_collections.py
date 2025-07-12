"""
Qdrant Collection Listing Tool
==============================

This script lists all collections in a Qdrant vector database, showing collection names
and basic information about each collection.

Usage:
    python list_qdrant_collections.py

Examples:
    # List collections from Qdrant server (defined in config.yaml and .env)
    python list_qdrant_collections.py

Notes:
    - The script uses server connection details from config.yaml and .env file
    - This script is part of a suite of Qdrant utilities:
      * create_qa_pairs_collection_qdrant.py - Creates and populates collections
      * query_qa_pairs_qdrant.py - Queries collections with semantic search
      * list_qdrant_collections.py - Lists available collections
      * delete_qdrant_collection.py - Deletes collections
"""

import yaml
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from qdrant_client import QdrantClient

def load_config():
    """Load configuration files from project root"""
    # Get the directory of this script
    script_dir = Path(__file__).resolve().parent
    
    # Get the project root (2 levels up: scripts -> qdrant -> project_root)
    project_root = script_dir.parents[1]
    
    # Load main config.yaml
    config_path = project_root / "config" / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found at {config_path}")
    
    print(f"Loading config from: {config_path}")
    config = yaml.safe_load(config_path.read_text())
    
    # Load datasources.yaml
    datasources_path = project_root / "config" / "datasources.yaml"
    if not datasources_path.exists():
        raise FileNotFoundError(f"Datasources config file not found at {datasources_path}")
    
    print(f"Loading datasources config from: {datasources_path}")
    datasources_config = yaml.safe_load(datasources_path.read_text())
    
    # Load embeddings.yaml
    embeddings_path = project_root / "config" / "embeddings.yaml"
    if not embeddings_path.exists():
        raise FileNotFoundError(f"Embeddings config file not found at {embeddings_path}")
    
    print(f"Loading embeddings config from: {embeddings_path}")
    embeddings_config = yaml.safe_load(embeddings_path.read_text())
    
    # Merge datasources and embeddings into main config
    config['datasources'] = datasources_config['datasources']
    config['embeddings'] = embeddings_config['embeddings']
    
    return config

def resolve_env_placeholder(value):
    """Resolve environment variable placeholders like ${VAR_NAME}"""
    if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
        env_var = value[2:-1]  # Remove ${ and }
        return os.getenv(env_var, value)  # Return original if env var not found
    return value

def get_qdrant_config():
    """Get Qdrant configuration with proper fallbacks"""
    # Load environment variables from main project directory
    project_env_path = Path(__file__).resolve().parents[2] / ".env"
    if project_env_path.exists():
        load_dotenv(project_env_path, override=True)
        print(f"Loading environment variables from: {project_env_path}")
    else:
        print(f"Warning: .env file not found at {project_env_path}")
    
    # Load configuration
    config = load_config()
    
    # Get Qdrant config with fallbacks
    qdrant_config = config.get('datasources', {}).get('qdrant', {})
    
    # Resolve environment variable placeholders
    host = resolve_env_placeholder(qdrant_config.get('host', 'localhost'))
    port = resolve_env_placeholder(qdrant_config.get('port', 6333))
    
    # Debug output to show what values are being used
    print(f"Qdrant config from config.yaml: host={qdrant_config.get('host')}, port={qdrant_config.get('port')}")
    print(f"Resolved values: host={host}, port={port}")
    
    # Convert port to int if it's a string
    if isinstance(port, str):
        try:
            port = int(port)
        except ValueError:
            print(f"Warning: Invalid port value '{port}', using default port 6333")
            port = 6333
    
    return host, port

def list_collections():
    # Get Qdrant connection details
    qdrant_host, qdrant_port = get_qdrant_config()
    
    print(f"Connecting to Qdrant server at: {qdrant_host}:{qdrant_port}")
    
    try:
        # Create Qdrant client
        client = QdrantClient(
            host=qdrant_host,
            port=qdrant_port,
            timeout=30
        )
        
        # Get list of all collections
        collections_response = client.get_collections()
        collections = collections_response.collections
        
        # Print collection information
        print("\nAvailable collections:")
        if not collections:
            print("No collections found.")
        else:
            for collection in collections:
                try:
                    # Get detailed collection info
                    collection_info = client.get_collection(collection.name)
                    vectors_count = collection_info.points_count
                    vector_size = collection_info.config.params.vectors.size
                    distance = collection_info.config.params.vectors.distance
                    
                    print(f"- {collection.name}")
                    print(f"  Vectors: {vectors_count}")
                    print(f"  Dimensions: {vector_size}")
                    print(f"  Distance: {distance}")
                    print()
                except Exception as e:
                    print(f"- {collection.name} (Error getting details: {str(e)})")
                    
    except Exception as e:
        print(f"Error connecting to Qdrant server: {str(e)}")
        print("Please check your connection details and ensure the Qdrant server is running.")

if __name__ == "__main__":
    list_collections()