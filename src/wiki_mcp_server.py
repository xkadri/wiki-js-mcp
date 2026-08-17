#!/usr/bin/env python3
"""Wiki.js MCP server using FastMCP - GraphQL version."""

import os
from dotenv import load_dotenv
load_dotenv()

import sys
import datetime
import json
import hashlib
import logging
import ast
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from dataclasses import dataclass

import httpx
from fastmcp import FastMCP
from slugify import slugify
import markdown
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from tenacity import retry, stop_after_attempt, wait_exponential
from pydantic import Field
from pydantic_settings import BaseSettings

# Create FastMCP server
mcp = FastMCP("Wiki.js Integration")

# Configuration
class Settings(BaseSettings):
    WIKIJS_API_URL: str = Field(default="http://localhost:3000")
    WIKIJS_TOKEN: Optional[str] = Field(default=None)
    WIKIJS_API_KEY: Optional[str] = Field(default=None)  # Alternative name for token
    WIKIJS_USERNAME: Optional[str] = Field(default=None)
    WIKIJS_PASSWORD: Optional[str] = Field(default=None)
    WIKIJS_MCP_DB: str = Field(default="./wikijs_mappings.db")
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE: str = Field(default="wikijs_mcp.log")
    REPOSITORY_ROOT: str = Field(default="./")
    DEFAULT_SPACE_NAME: str = Field(default="Documentation")
    
    class Config:
        env_file = ".env"
        extra = "ignore"  # Allow extra fields without validation errors
    
    @property
    def token(self) -> Optional[str]:
        """Get the token from either WIKIJS_TOKEN or WIKIJS_API_KEY."""
        return self.WIKIJS_TOKEN or self.WIKIJS_API_KEY

settings = Settings()

# Setup logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(settings.LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Database models
Base = declarative_base()

class FileMapping(Base):
    __tablename__ = 'file_mappings'
    
    id = Column(Integer, primary_key=True)
    file_path = Column(String, unique=True, nullable=False)
    page_id = Column(Integer, nullable=False)
    relationship_type = Column(String, nullable=False)
    last_updated = Column(DateTime, default=datetime.datetime.utcnow)
    file_hash = Column(String)
    repository_root = Column(String, default='')
    space_name = Column(String, default='')

class RepositoryContext(Base):
    __tablename__ = 'repository_contexts'
    
    id = Column(Integer, primary_key=True)
    root_path = Column(String, unique=True, nullable=False)
    space_name = Column(String, nullable=False)
    space_id = Column(Integer)
    last_updated = Column(DateTime, default=datetime.datetime.utcnow)

# Database setup
engine = create_engine(f"sqlite:///{settings.WIKIJS_MCP_DB}")
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class WikiJSClient:
    """Wiki.js GraphQL API client for handling requests."""
    
    def __init__(self):
        self.base_url = settings.WIKIJS_API_URL.rstrip('/')
        self.client = httpx.AsyncClient(timeout=30.0)
        self.authenticated = False
        
    async def authenticate(self) -> bool:
        """Set up authentication headers for GraphQL requests."""
        if settings.token:
            self.client.headers.update({
                "Authorization": f"Bearer {settings.token}",
                "Content-Type": "application/json"
            })
            self.authenticated = True
            return True
        elif settings.WIKIJS_USERNAME and settings.WIKIJS_PASSWORD:
            # For username/password, we need to login via GraphQL mutation
            try:
                login_mutation = """
                mutation($username: String!, $password: String!) {
                    authentication {
                        login(username: $username, password: $password) {
                            succeeded
                            jwt
                            message
                        }
                    }
                }
                """
                
                response = await self.graphql_request(login_mutation, {
                    "username": settings.WIKIJS_USERNAME,
                    "password": settings.WIKIJS_PASSWORD
                })
                
                if response.get("data", {}).get("authentication", {}).get("login", {}).get("succeeded"):
                    jwt_token = response["data"]["authentication"]["login"]["jwt"]
                    self.client.headers.update({
                        "Authorization": f"Bearer {jwt_token}",
                        "Content-Type": "application/json"
                    })
                    self.authenticated = True
                    return True
                else:
                    logger.error(f"Login failed: {response}")
                    return False
            except Exception as e:
                logger.error(f"Authentication failed: {e}")
                return False
        return False
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    async def graphql_request(self, query: str, variables: Dict = None) -> Dict:
        """Make GraphQL request to Wiki.js."""
        url = f"{self.base_url}/graphql"
        
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        
        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            
            data = response.json()
            
            # Check for GraphQL errors
            if "errors" in data:
                error_msg = "; ".join([err.get("message", str(err)) for err in data["errors"]])
                raise Exception(f"GraphQL error: {error_msg}")
            
            return data
        except httpx.HTTPStatusError as e:
            logger.error(f"Wiki.js GraphQL HTTP error {e.response.status_code}: {e.response.text}")
            raise Exception(f"Wiki.js GraphQL HTTP error {e.response.status_code}: {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Wiki.js connection error: {str(e)}")
            raise Exception(f"Wiki.js connection error: {str(e)}")

# Initialize client
wikijs = WikiJSClient()

def get_db():
    """Get database session."""
    db = SessionLocal()
    try:
        return db
    finally:
        db.close()

def get_file_hash(file_path: str) -> str:
    """Calculate SHA256 hash of file content."""
    try:
        with open(file_path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return ""

def markdown_to_html(content: str) -> str:
    """Convert markdown content to HTML."""
    md = markdown.Markdown(extensions=['codehilite', 'fenced_code', 'tables'])
    return md.convert(content)

def find_repository_root(start_path: str = None) -> Optional[str]:
    """Find the repository root by looking for .git directory or .wikijs_mcp file."""
    if start_path is None:
        start_path = os.getcwd()
    
    current_path = Path(start_path).resolve()
    
    # Walk up the directory tree
    for path in [current_path] + list(current_path.parents):
        # Check for .git directory (Git repository)
        if (path / '.git').exists():
            return str(path)
        # Check for .wikijs_mcp file (explicit Wiki.js repository marker)
        if (path / '.wikijs_mcp').exists():
            return str(path)
    
    # If no repository markers found, use current directory
    return str(current_path)

def extract_code_structure(file_path: str) -> Dict[str, Any]:
    """Extract classes and functions from Python files using AST."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        tree = ast.parse(content)
        structure = {
            'classes': [],
            'functions': [],
            'imports': []
        }
        
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                structure['classes'].append({
                    'name': node.name,
                    'line': node.lineno,
                    'docstring': ast.get_docstring(node)
                })
            elif isinstance(node, ast.FunctionDef):
                structure['functions'].append({
                    'name': node.name,
                    'line': node.lineno,
                    'docstring': ast.get_docstring(node)
                })
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        structure['imports'].append(alias.name)
                else:
                    module = node.module or ''
                    for alias in node.names:
                        structure['imports'].append(f"{module}.{alias.name}")
        
        return structure
    except Exception as e:
        logger.error(f"Error parsing {file_path}: {e}")
        return {'classes': [], 'functions': [], 'imports': []}

# MCP Tools Implementation

@mcp.tool()
async def wikijs_create_page(title: str, content: str, space_id: str = "", parent_id: str = "") -> str:
    """
    Create a new page in Wiki.js with support for hierarchical organization.
    
    Args:
        title: Page title
        content: Page content (markdown or HTML)
        space_id: Space ID (optional, uses default if not provided)
        parent_id: Parent page ID for hierarchical organization (optional)
    
    Returns:
        JSON string with page details: {'pageId': int, 'url': str}
    """
    try:
        await wikijs.authenticate()
        
        # Generate path - if parent_id provided, create nested path
        if parent_id:
            # Get parent page to build nested path
            parent_query = """
            query($id: Int!) {
                pages {
                    single(id: $id) {
                        path
                        title
                    }
                }
            }
            """
            parent_response = await wikijs.graphql_request(parent_query, {"id": int(parent_id)})
            parent_data = parent_response.get("data", {}).get("pages", {}).get("single")
            
            if parent_data:
                parent_path = parent_data["path"]
                # Create nested path: parent-path/child-title
                path = f"{parent_path}/{slugify(title)}"
            else:
                path = slugify(title)
        else:
            path = slugify(title)
        
        # GraphQL mutation to create a page
        mutation = """
        mutation($content: String!, $description: String!, $editor: String!, $isPublished: Boolean!, $isPrivate: Boolean!, $locale: String!, $path: String!, $publishEndDate: Date, $publishStartDate: Date, $scriptCss: String, $scriptJs: String, $tags: [String]!, $title: String!) {
            pages {
                create(content: $content, description: $description, editor: $editor, isPublished: $isPublished, isPrivate: $isPrivate, locale: $locale, path: $path, publishEndDate: $publishEndDate, publishStartDate: $publishStartDate, scriptCss: $scriptCss, scriptJs: $scriptJs, tags: $tags, title: $title) {
                    responseResult {
                        succeeded
                        errorCode
                        slug
                        message
                    }
                    page {
                        id
                        path
                        title
                    }
                }
            }
        }
        """
        
        variables = {
            "content": content,
            "description": "",
            "editor": "markdown",
            "isPublished": True,
            "isPrivate": False,
            "locale": "en",
            "path": path,
            "publishEndDate": None,
            "publishStartDate": None,
            "scriptCss": "",
            "scriptJs": "",
            "tags": [],
            "title": title
        }
        
        response = await wikijs.graphql_request(mutation, variables)
        
        create_result = response.get("data", {}).get("pages", {}).get("create", {})
        response_result = create_result.get("responseResult", {})
        
        if response_result.get("succeeded"):
            page_data = create_result.get("page", {})
            result = {
                "pageId": page_data.get("id"),
                "url": page_data.get("path"),
                "title": page_data.get("title"),
                "status": "created",
                "parentId": int(parent_id) if parent_id else None,
                "hierarchicalPath": path
            }
            logger.info(f"Created page: {title} (ID: {result['pageId']}) at path: {path}")
            return json.dumps(result)
        else:
            error_msg = response_result.get("message", "Unknown error")
            return json.dumps({"error": f"Failed to create page: {error_msg}"})
        
    except Exception as e:
        error_msg = f"Failed to create page '{title}': {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_update_page(page_id: int, title: str = None, content: str = None) -> str:
    """
    Update an existing page in Wiki.js.
    
    Args:
        page_id: Page ID to update
        title: New title (optional)
        content: New content (optional)
    
    Returns:
        JSON string with update status
    """
    try:
        await wikijs.authenticate()
        
        # First get the current page data
        get_query = """
        query($id: Int!) {
            pages {
                single(id: $id) {
                    id
                    path
                    title
                    content
                    description
                    isPrivate
                    isPublished
                    locale
                    tags {
                        tag
                    }
                }
            }
        }
        """
        
        get_response = await wikijs.graphql_request(get_query, {"id": page_id})
        current_page = get_response.get("data", {}).get("pages", {}).get("single")
        
        if not current_page:
            return json.dumps({"error": f"Page with ID {page_id} not found"})
        
        # GraphQL mutation to update a page
        mutation = """
        mutation($id: Int!, $content: String!, $description: String!, $editor: String!, $isPrivate: Boolean!, $isPublished: Boolean!, $locale: String!, $path: String!, $scriptCss: String, $scriptJs: String, $tags: [String]!, $title: String!) {
            pages {
                update(id: $id, content: $content, description: $description, editor: $editor, isPrivate: $isPrivate, isPublished: $isPublished, locale: $locale, path: $path, scriptCss: $scriptCss, scriptJs: $scriptJs, tags: $tags, title: $title) {
                    responseResult {
                        succeeded
                        errorCode
                        slug
                        message
                    }
                    page {
                        id
                        path
                        title
                        updatedAt
                    }
                }
            }
        }
        """
        
        # Use provided values or keep current ones
        new_title = title if title is not None else current_page["title"]
        new_content = content if content is not None else current_page["content"]
        
        variables = {
            "id": page_id,
            "content": new_content,
            "description": current_page.get("description", ""),
            "editor": "markdown",
            "isPrivate": current_page.get("isPrivate", False),
            "isPublished": current_page.get("isPublished", True),
            "locale": current_page.get("locale", "en"),
            "path": current_page["path"],
            "scriptCss": "",
            "scriptJs": "",
            "tags": [tag["tag"] for tag in current_page.get("tags", [])],
            "title": new_title
        }
        
        response = await wikijs.graphql_request(mutation, variables)
        
        update_result = response.get("data", {}).get("pages", {}).get("update", {})
        response_result = update_result.get("responseResult", {})
        
        if response_result.get("succeeded"):
            page_data = update_result.get("page", {})
            result = {
                "pageId": page_id,
                "status": "updated",
                "title": page_data.get("title"),
                "lastModified": page_data.get("updatedAt")
            }
            logger.info(f"Updated page ID: {page_id}")
            return json.dumps(result)
        else:
            error_msg = response_result.get("message", "Unknown error")
            return json.dumps({"error": f"Failed to update page: {error_msg}"})
        
    except Exception as e:
        error_msg = f"Failed to update page {page_id}: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_get_page(page_id: int = None, slug: str = None, pageId: int = None) -> str:
    """
    Retrieve page metadata and content from Wiki.js.

    Args:
        page_id: Page ID (optional)
        slug: Page slug/path (optional)
        pageId: Alias for page_id (accepted because other tools in this
            server return "pageId" in their JSON output, and callers
            naturally reuse that field name as the next call's argument)

    Returns:
        JSON string with page data
    """
    page_id = page_id if page_id is not None else pageId
    try:
        await wikijs.authenticate()
        
        if page_id:
            query = """
            query($id: Int!) {
                pages {
                    single(id: $id) {
                        id
                        path
                        title
                        content
                        description
                        isPrivate
                        isPublished
                        locale
                        createdAt
                        updatedAt
                        tags {
                            tag
                        }
                    }
                }
            }
            """
            variables = {"id": page_id}
        elif slug:
            query = """
            query($path: String!) {
                pages {
                    singleByPath(path: $path, locale: "en") {
                        id
                        path
                        title
                        content
                        description
                        isPrivate
                        isPublished
                        locale
                        createdAt
                        updatedAt
                        tags {
                            tag
                        }
                    }
                }
            }
            """
            variables = {"path": slug}
        else:
            return json.dumps({"error": "Either page_id or slug must be provided"})
        
        response = await wikijs.graphql_request(query, variables)
        
        page_data = None
        if page_id:
            page_data = response.get("data", {}).get("pages", {}).get("single")
        else:
            page_data = response.get("data", {}).get("pages", {}).get("singleByPath")
        
        if not page_data:
            return json.dumps({"error": "Page not found"})
        
        result = {
            "pageId": page_data.get("id"),
            "title": page_data.get("title"),
            "content": page_data.get("content"),
            "contentType": "markdown",
            "lastModified": page_data.get("updatedAt"),
            "path": page_data.get("path"),
            "isPublished": page_data.get("isPublished"),
            "description": page_data.get("description"),
            "tags": [tag["tag"] for tag in page_data.get("tags", [])]
        }
        
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Failed to get page: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_add_comment(page_id: int, content: str, pageId: int = None) -> str:
    """
    Add a comment to a Wiki.js page.

    Args:
        page_id: Page ID to comment on
        content: Comment text (Markdown supported)
        pageId: Alias for page_id (accepted because other tools in this
            server return "pageId" in their JSON output, and callers
            naturally reuse that field name as the next call's argument)

    Returns:
        JSON string with the created comment's ID, or an error
    """
    page_id = page_id if page_id is not None else pageId
    try:
        await wikijs.authenticate()

        mutation = """
        mutation($pageId: Int!, $content: String!) {
            comments {
                create(pageId: $pageId, content: $content) {
                    responseResult { succeeded errorCode message }
                    id
                }
            }
        }
        """

        response = await wikijs.graphql_request(mutation, {"pageId": page_id, "content": content})
        result = response.get("data", {}).get("comments", {}).get("create", {})
        response_result = result.get("responseResult", {})

        if response_result.get("succeeded"):
            return json.dumps({
                "commentId": result.get("id"),
                "pageId": page_id,
                "status": "created"
            })
        else:
            error_msg = response_result.get("message", "Unknown error")
            return json.dumps({"error": f"Failed to add comment: {error_msg}"})

    except Exception as e:
        error_msg = f"Failed to add comment to page {page_id}: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_search_pages(query: str, space_id: str = None) -> str:
    """
    Search pages by text in Wiki.js.
    
    Args:
        query: Search query
        space_id: Space ID to limit search (optional)
    
    Returns:
        JSON string with search results
    """
    try:
        await wikijs.authenticate()
        
        # GraphQL query for search (fixed - removed invalid suggestions subfields)
        search_query = """
        query($query: String!) {
            pages {
                search(query: $query, path: "", locale: "en") {
                    results {
                        id
                        title
                        description
                        path
                        locale
                    }
                    totalHits
                }
            }
        }
        """
        
        variables = {"query": query}
        
        response = await wikijs.graphql_request(search_query, variables)
        
        search_data = response.get("data", {}).get("pages", {}).get("search", {})
        
        results = []
        for item in search_data.get("results", []):
            results.append({
                "pageId": item.get("id"),
                "title": item.get("title"),
                "snippet": item.get("description", ""),
                "score": 1.0,  # Wiki.js doesn't provide scores
                "path": item.get("path")
            })
        
        return json.dumps({
            "results": results, 
            "total": search_data.get("totalHits", len(results))
        })
        
    except Exception as e:
        error_msg = f"Search failed: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_list_pages() -> str:
    """
    List every page in Wiki.js as minimal (id, path) pairs, sorted by path.
    Deliberately excludes title/description/timestamps to stay small even
    on a large wiki -- call wikijs_get_page for full detail on a specific
    page once you have picked its id. Use this to enumerate all pages
    before picking one deterministically -- there is no random-sample tool.

    Returns:
        JSON string with the minimal page list and total count
    """
    try:
        await wikijs.authenticate()

        all_pages_query = """
        query {
            pages {
                list {
                    id
                    path
                }
            }
        }
        """

        response = await wikijs.graphql_request(all_pages_query)
        all_pages = response.get("data", {}).get("pages", {}).get("list", [])

        pages = sorted(
            (
                {"pageId": page["id"], "path": page["path"]}
                for page in all_pages
            ),
            key=lambda p: p["path"]
        )

        return json.dumps({"pages": pages, "total": len(pages)})

    except Exception as e:
        error_msg = f"Failed to list pages: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_list_spaces() -> str:
    """
    List all spaces (top-level Wiki.js containers).
    Note: Wiki.js doesn't have "spaces" like BookStack, but we can list pages at root level.
    
    Returns:
        JSON string with spaces list
    """
    try:
        await wikijs.authenticate()
        
        # Get all pages and group by top-level paths
        query = """
        query {
            pages {
                list {
                    id
                    title
                    path
                    description
                    isPublished
                    locale
                }
            }
        }
        """
        
        response = await wikijs.graphql_request(query)
        
        pages = response.get("data", {}).get("pages", {}).get("list", [])
        
        # Group pages by top-level path (simulate spaces)
        spaces = {}
        for page in pages:
            path_parts = page["path"].split("/")
            if len(path_parts) > 0:
                top_level = path_parts[0] if path_parts[0] else "root"
                if top_level not in spaces:
                    spaces[top_level] = {
                        "spaceId": hash(top_level) % 10000,  # Generate pseudo ID
                        "name": top_level.replace("-", " ").title(),
                        "slug": top_level,
                        "description": f"Pages under /{top_level}",
                        "pageCount": 0
                    }
                spaces[top_level]["pageCount"] += 1
        
        return json.dumps({"spaces": list(spaces.values())})
        
    except Exception as e:
        error_msg = f"Failed to list spaces: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_create_space(name: str, description: str = None) -> str:
    """
    Create a new space in Wiki.js.
    Note: Wiki.js doesn't have spaces, so this creates a root-level page as a space placeholder.
    
    Args:
        name: Space name
        description: Space description (optional)
    
    Returns:
        JSON string with space details
    """
    try:
        # Create a root page that acts as a space
        space_content = f"# {name}\n\n{description or 'This is the main page for the ' + name + ' section.'}\n\n## Pages in this section:\n\n*Pages will be listed here as they are created.*"
        
        result = await wikijs_create_page(name, space_content)
        result_data = json.loads(result)
        
        if "error" not in result_data:
            # Convert page result to space format
            space_result = {
                "spaceId": result_data.get("pageId"),
                "name": name,
                "slug": slugify(name),
                "status": "created",
                "description": description
            }
            logger.info(f"Created space (root page): {name} (ID: {space_result['spaceId']})")
            return json.dumps(space_result)
        else:
            return result
        
    except Exception as e:
        error_msg = f"Failed to create space '{name}': {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_link_file_to_page(file_path: str, page_id: int, relationship: str = "documents") -> str:
    """
    Persist link between code file and Wiki.js page in local database.
    
    Args:
        file_path: Path to the source file
        page_id: Wiki.js page ID
        relationship: Type of relationship (documents, references, etc.)
    
    Returns:
        JSON string with link status
    """
    try:
        db = get_db()
        
        # Calculate file hash
        file_hash = get_file_hash(file_path)
        repo_root = find_repository_root(file_path)
        
        # Create or update mapping
        mapping = db.query(FileMapping).filter(FileMapping.file_path == file_path).first()
        if mapping:
            mapping.page_id = page_id
            mapping.relationship_type = relationship
            mapping.file_hash = file_hash
            mapping.last_updated = datetime.datetime.utcnow()
        else:
            mapping = FileMapping(
                file_path=file_path,
                page_id=page_id,
                relationship_type=relationship,
                file_hash=file_hash,
                repository_root=repo_root or ""
            )
            db.add(mapping)
        
        db.commit()
        
        result = {
            "linked": True,
            "file_path": file_path,
            "page_id": page_id,
            "relationship": relationship
        }
        
        logger.info(f"Linked file {file_path} to page {page_id}")
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Failed to link file to page: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_sync_file_docs(file_path: str, change_summary: str, snippet: str = None) -> str:
    """
    Sync a code change to the linked Wiki.js page.
    
    Args:
        file_path: Path to the changed file
        change_summary: Summary of changes made
        snippet: Code snippet showing changes (optional)
    
    Returns:
        JSON string with sync status
    """
    try:
        db = get_db()
        
        # Look up page mapping
        mapping = db.query(FileMapping).filter(FileMapping.file_path == file_path).first()
        if not mapping:
            return json.dumps({"error": f"No page mapping found for {file_path}"})
        
        # Get current page content
        page_response = await wikijs_get_page(page_id=mapping.page_id)
        page_data = json.loads(page_response)
        
        if "error" in page_data:
            return json.dumps({"error": f"Failed to get page: {page_data['error']}"})
        
        # Append change summary to page content
        current_content = page_data.get("content", "")
        
        update_section = f"\n\n## Recent Changes\n\n**{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}**: {change_summary}\n"
        if snippet:
            update_section += f"\n```\n{snippet}\n```\n"
        
        new_content = current_content + update_section
        
        # Update the page
        update_response = await wikijs_update_page(mapping.page_id, content=new_content)
        update_data = json.loads(update_response)
        
        if "error" in update_data:
            return json.dumps({"error": f"Failed to update page: {update_data['error']}"})
        
        # Update file hash
        mapping.file_hash = get_file_hash(file_path)
        mapping.last_updated = datetime.datetime.utcnow()
        db.commit()
        
        result = {
            "updated": True,
            "file_path": file_path,
            "page_id": mapping.page_id,
            "change_summary": change_summary
        }
        
        logger.info(f"Synced changes from {file_path} to page {mapping.page_id}")
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Failed to sync file docs: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_generate_file_overview(
    file_path: str, 
    include_functions: bool = True, 
    include_classes: bool = True,
    include_dependencies: bool = True,
    include_examples: bool = False,
    target_page_id: int = None
) -> str:
    """
    Create or update a structured overview page for a file.
    
    Args:
        file_path: Path to the source file
        include_functions: Include function documentation
        include_classes: Include class documentation
        include_dependencies: Include import/dependency information
        include_examples: Include usage examples
        target_page_id: Specific page ID to update (optional)
    
    Returns:
        JSON string with overview page details
    """
    try:
        if not os.path.exists(file_path):
            return json.dumps({"error": f"File not found: {file_path}"})
        
        # Extract code structure
        structure = extract_code_structure(file_path)
        
        # Generate documentation content
        content_parts = [f"# {os.path.basename(file_path)} Overview\n"]
        content_parts.append(f"**File Path**: `{file_path}`\n")
        content_parts.append(f"**Last Updated**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        
        if include_dependencies and structure['imports']:
            content_parts.append("\n## Dependencies\n")
            for imp in structure['imports']:
                content_parts.append(f"- `{imp}`")
            content_parts.append("")
        
        if include_classes and structure['classes']:
            content_parts.append("\n## Classes\n")
            for cls in structure['classes']:
                content_parts.append(f"### {cls['name']} (Line {cls['line']})\n")
                if cls['docstring']:
                    content_parts.append(f"{cls['docstring']}\n")
        
        if include_functions and structure['functions']:
            content_parts.append("\n## Functions\n")
            for func in structure['functions']:
                content_parts.append(f"### {func['name']}() (Line {func['line']})\n")
                if func['docstring']:
                    content_parts.append(f"{func['docstring']}\n")
        
        if include_examples:
            content_parts.append("\n## Usage Examples\n")
            content_parts.append("```python\n# Add usage examples here\n```\n")
        
        content = "\n".join(content_parts)
        
        # Create or update page
        if target_page_id:
            # Update existing page
            response = await wikijs_update_page(target_page_id, content=content)
            result_data = json.loads(response)
            if "error" not in result_data:
                result_data["action"] = "updated"
        else:
            # Create new page
            title = f"{os.path.basename(file_path)} Documentation"
            response = await wikijs_create_page(title, content)
            result_data = json.loads(response)
            if "error" not in result_data:
                result_data["action"] = "created"
                # Link file to new page
                if "pageId" in result_data:
                    await wikijs_link_file_to_page(file_path, result_data["pageId"], "documents")
        
        logger.info(f"Generated overview for {file_path}")
        return json.dumps(result_data)
        
    except Exception as e:
        error_msg = f"Failed to generate file overview: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_bulk_update_project_docs(
    summary: str, 
    affected_files: List[str], 
    context: str,
    auto_create_missing: bool = True
) -> str:
    """
    Batch update pages for large changes across multiple files.
    
    Args:
        summary: Overall project change summary
        affected_files: List of file paths that were changed
        context: Additional context about the changes
        auto_create_missing: Create pages for files without mappings
    
    Returns:
        JSON string with bulk update results
    """
    try:
        db = get_db()
        results = {
            "updated_pages": [],
            "created_pages": [],
            "errors": []
        }
        
        # Process each affected file
        for file_path in affected_files:
            try:
                # Check if file has a mapping
                mapping = db.query(FileMapping).filter(FileMapping.file_path == file_path).first()
                
                if mapping:
                    # Update existing page
                    sync_response = await wikijs_sync_file_docs(
                        file_path, 
                        f"Bulk update: {summary}", 
                        context
                    )
                    sync_data = json.loads(sync_response)
                    if "error" not in sync_data:
                        results["updated_pages"].append({
                            "file_path": file_path,
                            "page_id": mapping.page_id
                        })
                    else:
                        results["errors"].append({
                            "file_path": file_path,
                            "error": sync_data["error"]
                        })
                
                elif auto_create_missing:
                    # Create new overview page
                    overview_response = await wikijs_generate_file_overview(file_path)
                    overview_data = json.loads(overview_response)
                    if "error" not in overview_data and "pageId" in overview_data:
                        results["created_pages"].append({
                            "file_path": file_path,
                            "page_id": overview_data["pageId"]
                        })
                    else:
                        results["errors"].append({
                            "file_path": file_path,
                            "error": overview_data.get("error", "Failed to create page")
                        })
                
            except Exception as e:
                results["errors"].append({
                    "file_path": file_path,
                    "error": str(e)
                })
        
        results["summary"] = {
            "total_files": len(affected_files),
            "updated": len(results["updated_pages"]),
            "created": len(results["created_pages"]),
            "errors": len(results["errors"])
        }
        
        logger.info(f"Bulk update completed: {results['summary']}")
        return json.dumps(results)
        
    except Exception as e:
        error_msg = f"Bulk update failed: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_manage_collections(collection_name: str, description: str = None, space_ids: List[int] = None) -> str:
    """
    Manage Wiki.js collections (groups of spaces/pages).
    Note: This is a placeholder as Wiki.js collections API may vary by version.
    
    Args:
        collection_name: Name of the collection
        description: Collection description
        space_ids: List of space IDs to include
    
    Returns:
        JSON string with collection details
    """
    try:
        # This is a conceptual implementation
        # Actual Wiki.js API for collections may differ
        result = {
            "collection_name": collection_name,
            "description": description,
            "space_ids": space_ids or [],
            "status": "managed",
            "note": "Collection management depends on Wiki.js version and configuration"
        }
        
        logger.info(f"Managed collection: {collection_name}")
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Failed to manage collection: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_connection_status() -> str:
    """
    Check the status of the Wiki.js connection and authentication.
    
    Returns:
        JSON string with connection status
    """
    try:
        auth_success = await wikijs.authenticate()
        
        if auth_success:
            # Test with a simple API call
            response = await wikijs.graphql_request("query { pages { list { id } } }")
            
            result = {
                "connected": True,
                "authenticated": True,
                "api_url": settings.WIKIJS_API_URL,
                "auth_method": "token" if settings.token else "session",
                "status": "healthy"
            }
        else:
            result = {
                "connected": False,
                "authenticated": False,
                "api_url": settings.WIKIJS_API_URL,
                "status": "authentication_failed"
            }
        
        return json.dumps(result)
        
    except Exception as e:
        result = {
            "connected": False,
            "authenticated": False,
            "api_url": settings.WIKIJS_API_URL,
            "error": str(e),
            "status": "connection_failed"
        }
        return json.dumps(result)

@mcp.tool()
async def wikijs_repository_context() -> str:
    """
    Show current repository context and Wiki.js organization.
    
    Returns:
        JSON string with repository context
    """
    try:
        repo_root = find_repository_root()
        db = get_db()
        
        # Get repository context from database
        context = db.query(RepositoryContext).filter(
            RepositoryContext.root_path == repo_root
        ).first()
        
        # Get file mappings for this repository
        mappings = db.query(FileMapping).filter(
            FileMapping.repository_root == repo_root
        ).all()
        
        result = {
            "repository_root": repo_root,
            "space_name": context.space_name if context else settings.DEFAULT_SPACE_NAME,
            "space_id": context.space_id if context else None,
            "mapped_files": len(mappings),
            "mappings": [
                {
                    "file_path": m.file_path,
                    "page_id": m.page_id,
                    "relationship": m.relationship_type,
                    "last_updated": m.last_updated.isoformat() if m.last_updated else None
                }
                for m in mappings[:10]  # Limit to first 10 for brevity
            ]
        }
        
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Failed to get repository context: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_create_repo_structure(repo_name: str, description: str = None, sections: List[str] = None) -> str:
    """
    Create a complete repository documentation structure with nested pages.
    
    Args:
        repo_name: Repository name (will be the root page)
        description: Repository description
        sections: List of main sections to create (e.g., ["Overview", "API", "Components", "Deployment"])
    
    Returns:
        JSON string with created structure details
    """
    try:
        # Default sections if none provided
        if not sections:
            sections = ["Overview", "Getting Started", "Architecture", "API Reference", "Development", "Deployment"]
        
        # Create root repository page
        root_content = f"""# {repo_name}

{description or f'Documentation for the {repo_name} repository.'}

## Repository Structure

This documentation is organized into the following sections:

"""
        
        for section in sections:
            root_content += f"- [{section}]({slugify(repo_name)}/{slugify(section)})\n"
        
        root_content += f"""

## Quick Links

- [Repository Overview]({slugify(repo_name)}/overview)
- [Getting Started Guide]({slugify(repo_name)}/getting-started)
- [API Documentation]({slugify(repo_name)}/api-reference)

---
*This documentation structure was created by the Wiki.js MCP server.*
"""
        
        # Create root page
        root_result = await wikijs_create_page(repo_name, root_content)
        root_data = json.loads(root_result)
        
        if "error" in root_data:
            return json.dumps({"error": f"Failed to create root page: {root_data['error']}"})
        
        root_page_id = root_data["pageId"]
        created_pages = [root_data]
        
        # Create section pages
        for section in sections:
            section_content = f"""# {section}

This is the {section.lower()} section for {repo_name}.

## Contents

*Content will be added here as the documentation grows.*

## Related Pages

- [Back to {repo_name}]({slugify(repo_name)})

---
*This page is part of the {repo_name} documentation structure.*
"""
            
            section_result = await wikijs_create_page(section, section_content, parent_id=str(root_page_id))
            section_data = json.loads(section_result)
            
            if "error" not in section_data:
                created_pages.append(section_data)
            else:
                logger.warning(f"Failed to create section '{section}': {section_data['error']}")
        
        result = {
            "repository": repo_name,
            "root_page_id": root_page_id,
            "created_pages": len(created_pages),
            "sections": sections,
            "pages": created_pages,
            "status": "created"
        }
        
        logger.info(f"Created repository structure for {repo_name} with {len(created_pages)} pages")
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Failed to create repository structure: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_create_nested_page(title: str, content: str, parent_path: str, create_parent_if_missing: bool = True) -> str:
    """
    Create a nested page using hierarchical paths (e.g., "repo/api/endpoints").
    
    Args:
        title: Page title
        content: Page content
        parent_path: Full path to parent (e.g., "my-repo/api")
        create_parent_if_missing: Create parent pages if they don't exist
    
    Returns:
        JSON string with page details
    """
    try:
        await wikijs.authenticate()
        
        # Check if parent exists
        parent_query = """
        query($path: String!) {
            pages {
                singleByPath(path: $path, locale: "en") {
                    id
                    path
                    title
                }
            }
        }
        """
        
        parent_response = await wikijs.graphql_request(parent_query, {"path": parent_path})
        parent_data = parent_response.get("data", {}).get("pages", {}).get("singleByPath")
        
        if not parent_data and create_parent_if_missing:
            # Create parent structure
            path_parts = parent_path.split("/")
            current_path = ""
            parent_id = None
            
            for i, part in enumerate(path_parts):
                if current_path:
                    current_path += f"/{part}"
                else:
                    current_path = part
                
                # Check if this level exists
                check_response = await wikijs.graphql_request(parent_query, {"path": current_path})
                existing = check_response.get("data", {}).get("pages", {}).get("singleByPath")
                
                if not existing:
                    # Create this level
                    part_title = part.replace("-", " ").title()
                    part_content = f"""# {part_title}

This is a section page for organizing documentation.

## Subsections

*Subsections will appear here as they are created.*

---
*This page was auto-created as part of the documentation hierarchy.*
"""
                    
                    create_result = await wikijs_create_page(part_title, part_content, parent_id=str(parent_id) if parent_id else "")
                    create_data = json.loads(create_result)
                    
                    if "error" not in create_data:
                        parent_id = create_data["pageId"]
                    else:
                        return json.dumps({"error": f"Failed to create parent '{current_path}': {create_data['error']}"})
                else:
                    parent_id = existing["id"]
        
        elif parent_data:
            parent_id = parent_data["id"]
        else:
            return json.dumps({"error": f"Parent path '{parent_path}' not found and create_parent_if_missing is False"})
        
        # Create the target page
        result = await wikijs_create_page(title, content, parent_id=str(parent_id))
        result_data = json.loads(result)
        
        if "error" not in result_data:
            result_data["parent_path"] = parent_path
            result_data["full_path"] = f"{parent_path}/{slugify(title)}"
        
        return json.dumps(result_data)
        
    except Exception as e:
        error_msg = f"Failed to create nested page: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_get_page_children(page_id: int = None, page_path: str = None, pageId: int = None) -> str:
    """
    Get all child pages of a given page for hierarchical navigation.

    Args:
        page_id: Parent page ID (optional)
        page_path: Parent page path (optional)
        pageId: Alias for page_id (accepted because other tools in this
            server return "pageId" in their JSON output, and callers
            naturally reuse that field name as the next call's argument)

    Returns:
        JSON string with child pages list
    """
    page_id = page_id if page_id is not None else pageId
    try:
        await wikijs.authenticate()
        
        # Get the parent page first
        if page_id:
            parent_query = """
            query($id: Int!) {
                pages {
                    single(id: $id) {
                        id
                        path
                        title
                    }
                }
            }
            """
            parent_response = await wikijs.graphql_request(parent_query, {"id": page_id})
            parent_data = parent_response.get("data", {}).get("pages", {}).get("single")
        elif page_path:
            parent_query = """
            query($path: String!) {
                pages {
                    singleByPath(path: $path, locale: "en") {
                        id
                        path
                        title
                    }
                }
            }
            """
            parent_response = await wikijs.graphql_request(parent_query, {"path": page_path})
            parent_data = parent_response.get("data", {}).get("pages", {}).get("singleByPath")
        else:
            return json.dumps({"error": "Either page_id or page_path must be provided"})
        
        if not parent_data:
            return json.dumps({"error": "Parent page not found"})
        
        parent_path = parent_data["path"]
        
        # Get all pages and filter for children
        all_pages_query = """
        query {
            pages {
                list {
                    id
                    title
                    path
                    description
                    isPublished
                    updatedAt
                }
            }
        }
        """
        
        response = await wikijs.graphql_request(all_pages_query)
        all_pages = response.get("data", {}).get("pages", {}).get("list", [])
        
        # Filter for direct children (path starts with parent_path/ but no additional slashes)
        children = []
        for page in all_pages:
            page_path_str = page["path"]
            if page_path_str.startswith(f"{parent_path}/"):
                # Check if it's a direct child (no additional slashes after parent)
                remaining_path = page_path_str[len(parent_path) + 1:]
                if "/" not in remaining_path:  # Direct child
                    children.append({
                        "pageId": page["id"],
                        "title": page["title"],
                        "path": page["path"],
                        "description": page.get("description", ""),
                        "lastModified": page.get("updatedAt"),
                        "isPublished": page.get("isPublished", True)
                    })
        
        result = {
            "parent": {
                "pageId": parent_data["id"],
                "title": parent_data["title"],
                "path": parent_data["path"]
            },
            "children": children,
            "total_children": len(children)
        }
        
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Failed to get page children: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_create_documentation_hierarchy(project_name: str, file_mappings: List[Dict[str, str]], auto_organize: bool = True) -> str:
    """
    Create a complete documentation hierarchy for a project based on file structure.
    
    Args:
        project_name: Name of the project/repository
        file_mappings: List of {"file_path": "src/components/Button.tsx", "doc_path": "components/button"} mappings
        auto_organize: Automatically organize files into logical sections
    
    Returns:
        JSON string with created hierarchy details
    """
    try:
        # Auto-organize files into sections if requested
        if auto_organize:
            sections = {
                "components": [],
                "api": [],
                "utils": [],
                "services": [],
                "models": [],
                "tests": [],
                "config": [],
                "docs": []
            }
            
            for mapping in file_mappings:
                file_path = mapping["file_path"].lower()
                
                if "component" in file_path or "/components/" in file_path:
                    sections["components"].append(mapping)
                elif "api" in file_path or "/api/" in file_path or "endpoint" in file_path:
                    sections["api"].append(mapping)
                elif "util" in file_path or "/utils/" in file_path or "/helpers/" in file_path:
                    sections["utils"].append(mapping)
                elif "service" in file_path or "/services/" in file_path:
                    sections["services"].append(mapping)
                elif "model" in file_path or "/models/" in file_path or "/types/" in file_path:
                    sections["models"].append(mapping)
                elif "test" in file_path or "/tests/" in file_path or ".test." in file_path:
                    sections["tests"].append(mapping)
                elif "config" in file_path or "/config/" in file_path or ".config." in file_path:
                    sections["config"].append(mapping)
                else:
                    sections["docs"].append(mapping)
        
        # Create root project structure
        section_names = [name.title() for name, files in sections.items() if files] if auto_organize else ["Documentation"]
        
        repo_result = await wikijs_create_repo_structure(project_name, f"Documentation for {project_name}", section_names)
        repo_data = json.loads(repo_result)
        
        if "error" in repo_data:
            return repo_result
        
        created_pages = []
        created_mappings = []
        
        if auto_organize:
            # Create pages for each section
            for section_name, files in sections.items():
                if not files:
                    continue
                
                section_title = section_name.title()
                
                for file_mapping in files:
                    file_path = file_mapping["file_path"]
                    doc_path = file_mapping.get("doc_path", slugify(os.path.basename(file_path)))
                    
                    # Generate documentation content for the file
                    file_overview_result = await wikijs_generate_file_overview(file_path, target_page_id=None)
                    overview_data = json.loads(file_overview_result)
                    
                    if "error" not in overview_data:
                        created_pages.append(overview_data)
                        
                        # Create mapping
                        mapping_result = await wikijs_link_file_to_page(file_path, overview_data["pageId"], "documents")
                        mapping_data = json.loads(mapping_result)
                        
                        if "error" not in mapping_data:
                            created_mappings.append(mapping_data)
        else:
            # Create pages without auto-organization
            for file_mapping in file_mappings:
                file_path = file_mapping["file_path"]
                doc_path = file_mapping.get("doc_path", f"{project_name}/{slugify(os.path.basename(file_path))}")
                
                # Create nested page
                nested_result = await wikijs_create_nested_page(
                    os.path.basename(file_path),
                    f"# {os.path.basename(file_path)}\n\nDocumentation for {file_path}",
                    doc_path
                )
                nested_data = json.loads(nested_result)
                
                if "error" not in nested_data:
                    created_pages.append(nested_data)
                    
                    # Create mapping
                    mapping_result = await wikijs_link_file_to_page(file_path, nested_data["pageId"], "documents")
                    mapping_data = json.loads(mapping_result)
                    
                    if "error" not in mapping_data:
                        created_mappings.append(mapping_data)
        
        result = {
            "project": project_name,
            "root_structure": repo_data,
            "created_pages": len(created_pages),
            "created_mappings": len(created_mappings),
            "auto_organized": auto_organize,
            "sections": list(sections.keys()) if auto_organize else ["manual"],
            "pages": created_pages[:10],  # Limit output
            "mappings": created_mappings[:10],  # Limit output
            "status": "completed"
        }
        
        logger.info(f"Created documentation hierarchy for {project_name}: {len(created_pages)} pages, {len(created_mappings)} mappings")
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Failed to create documentation hierarchy: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_delete_page(page_id: int = None, page_path: str = None, remove_file_mapping: bool = True) -> str:
    """
    Delete a specific page from Wiki.js.
    
    Args:
        page_id: Page ID to delete (optional)
        page_path: Page path to delete (optional)
        remove_file_mapping: Also remove file-to-page mapping from local database
    
    Returns:
        JSON string with deletion status
    """
    try:
        await wikijs.authenticate()
        
        # Get page info first
        if page_id:
            get_query = """
            query($id: Int!) {
                pages {
                    single(id: $id) {
                        id
                        path
                        title
                    }
                }
            }
            """
            get_response = await wikijs.graphql_request(get_query, {"id": page_id})
            page_data = get_response.get("data", {}).get("pages", {}).get("single")
        elif page_path:
            get_query = """
            query($path: String!) {
                pages {
                    singleByPath(path: $path, locale: "en") {
                        id
                        path
                        title
                    }
                }
            }
            """
            get_response = await wikijs.graphql_request(get_query, {"path": page_path})
            page_data = get_response.get("data", {}).get("pages", {}).get("singleByPath")
            if page_data:
                page_id = page_data["id"]
        else:
            return json.dumps({"error": "Either page_id or page_path must be provided"})
        
        if not page_data:
            return json.dumps({"error": "Page not found"})
        
        # Delete the page using GraphQL mutation
        delete_mutation = """
        mutation($id: Int!) {
            pages {
                delete(id: $id) {
                    responseResult {
                        succeeded
                        errorCode
                        slug
                        message
                    }
                }
            }
        }
        """
        
        response = await wikijs.graphql_request(delete_mutation, {"id": page_id})
        
        delete_result = response.get("data", {}).get("pages", {}).get("delete", {})
        response_result = delete_result.get("responseResult", {})
        
        if response_result.get("succeeded"):
            result = {
                "deleted": True,
                "pageId": page_id,
                "title": page_data["title"],
                "path": page_data["path"],
                "status": "deleted"
            }
            
            # Remove file mapping if requested
            if remove_file_mapping:
                db = get_db()
                mapping = db.query(FileMapping).filter(FileMapping.page_id == page_id).first()
                if mapping:
                    db.delete(mapping)
                    db.commit()
                    result["file_mapping_removed"] = True
                else:
                    result["file_mapping_removed"] = False
            
            logger.info(f"Deleted page: {page_data['title']} (ID: {page_id})")
            return json.dumps(result)
        else:
            error_msg = response_result.get("message", "Unknown error")
            return json.dumps({"error": f"Failed to delete page: {error_msg}"})
        
    except Exception as e:
        error_msg = f"Failed to delete page: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_batch_delete_pages(
    page_ids: List[int] = None, 
    page_paths: List[str] = None,
    path_pattern: str = None,
    confirm_deletion: bool = False,
    remove_file_mappings: bool = True
) -> str:
    """
    Batch delete multiple pages from Wiki.js.
    
    Args:
        page_ids: List of page IDs to delete (optional)
        page_paths: List of page paths to delete (optional)
        path_pattern: Pattern to match paths (e.g., "frontend-app/*" for all pages under frontend-app)
        confirm_deletion: Must be True to actually delete pages (safety check)
        remove_file_mappings: Also remove file-to-page mappings from local database
    
    Returns:
        JSON string with batch deletion results
    """
    try:
        if not confirm_deletion:
            return json.dumps({
                "error": "confirm_deletion must be True to proceed with batch deletion",
                "safety_note": "This is a safety check to prevent accidental deletions"
            })
        
        await wikijs.authenticate()
        
        pages_to_delete = []
        
        # Collect pages by IDs
        if page_ids:
            for page_id in page_ids:
                get_query = """
                query($id: Int!) {
                    pages {
                        single(id: $id) {
                            id
                            path
                            title
                        }
                    }
                }
                """
                get_response = await wikijs.graphql_request(get_query, {"id": page_id})
                page_data = get_response.get("data", {}).get("pages", {}).get("single")
                if page_data:
                    pages_to_delete.append(page_data)
        
        # Collect pages by paths
        if page_paths:
            for page_path in page_paths:
                get_query = """
                query($path: String!) {
                    pages {
                        singleByPath(path: $path, locale: "en") {
                            id
                            path
                            title
                        }
                    }
                }
                """
                get_response = await wikijs.graphql_request(get_query, {"path": page_path})
                page_data = get_response.get("data", {}).get("pages", {}).get("singleByPath")
                if page_data:
                    pages_to_delete.append(page_data)
        
        # Collect pages by pattern
        if path_pattern:
            # Get all pages and filter by pattern
            all_pages_query = """
            query {
                pages {
                    list {
                        id
                        title
                        path
                    }
                }
            }
            """
            
            response = await wikijs.graphql_request(all_pages_query)
            all_pages = response.get("data", {}).get("pages", {}).get("list", [])
            
            # Simple pattern matching (supports * wildcard)
            import fnmatch
            for page in all_pages:
                if fnmatch.fnmatch(page["path"], path_pattern):
                    pages_to_delete.append(page)
        
        if not pages_to_delete:
            return json.dumps({"error": "No pages found to delete"})
        
        # Remove duplicates
        unique_pages = {}
        for page in pages_to_delete:
            unique_pages[page["id"]] = page
        pages_to_delete = list(unique_pages.values())
        
        # Delete pages
        deleted_pages = []
        failed_deletions = []
        
        for page in pages_to_delete:
            try:
                delete_result = await wikijs_delete_page(
                    page_id=page["id"], 
                    remove_file_mapping=remove_file_mappings
                )
                delete_data = json.loads(delete_result)
                
                if "error" not in delete_data:
                    deleted_pages.append({
                        "pageId": page["id"],
                        "title": page["title"],
                        "path": page["path"]
                    })
                else:
                    failed_deletions.append({
                        "pageId": page["id"],
                        "title": page["title"],
                        "path": page["path"],
                        "error": delete_data["error"]
                    })
            except Exception as e:
                failed_deletions.append({
                    "pageId": page["id"],
                    "title": page["title"],
                    "path": page["path"],
                    "error": str(e)
                })
        
        result = {
            "total_found": len(pages_to_delete),
            "deleted_count": len(deleted_pages),
            "failed_count": len(failed_deletions),
            "deleted_pages": deleted_pages,
            "failed_deletions": failed_deletions,
            "status": "completed"
        }
        
        logger.info(f"Batch deletion completed: {len(deleted_pages)} deleted, {len(failed_deletions)} failed")
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Batch deletion failed: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_delete_hierarchy(
    root_path: str,
    delete_mode: str = "children_only",
    confirm_deletion: bool = False,
    remove_file_mappings: bool = True
) -> str:
    """
    Delete an entire page hierarchy (folder structure) from Wiki.js.
    
    Args:
        root_path: Root path of the hierarchy to delete (e.g., "frontend-app" or "frontend-app/components")
        delete_mode: Deletion mode - "children_only", "include_root", or "root_only"
        confirm_deletion: Must be True to actually delete pages (safety check)
        remove_file_mappings: Also remove file-to-page mappings from local database
    
    Returns:
        JSON string with hierarchy deletion results
    """
    try:
        if not confirm_deletion:
            return json.dumps({
                "error": "confirm_deletion must be True to proceed with hierarchy deletion",
                "safety_note": "This is a safety check to prevent accidental deletions",
                "preview_mode": "Set confirm_deletion=True to actually delete"
            })
        
        valid_modes = ["children_only", "include_root", "root_only"]
        if delete_mode not in valid_modes:
            return json.dumps({
                "error": f"Invalid delete_mode. Must be one of: {valid_modes}"
            })
        
        await wikijs.authenticate()
        
        # Get all pages to find hierarchy
        all_pages_query = """
        query {
            pages {
                list {
                    id
                    title
                    path
                }
            }
        }
        """
        
        response = await wikijs.graphql_request(all_pages_query)
        all_pages = response.get("data", {}).get("pages", {}).get("list", [])
        
        # Find root page
        root_page = None
        for page in all_pages:
            if page["path"] == root_path:
                root_page = page
                break
        
        if not root_page and delete_mode in ["include_root", "root_only"]:
            return json.dumps({"error": f"Root page not found: {root_path}"})
        
        # Find child pages
        child_pages = []
        for page in all_pages:
            page_path = page["path"]
            if page_path.startswith(f"{root_path}/"):
                child_pages.append(page)
        
        # Determine pages to delete based on mode
        pages_to_delete = []
        
        if delete_mode == "children_only":
            pages_to_delete = child_pages
        elif delete_mode == "include_root":
            pages_to_delete = child_pages + ([root_page] if root_page else [])
        elif delete_mode == "root_only":
            pages_to_delete = [root_page] if root_page else []
        
        if not pages_to_delete:
            return json.dumps({
                "message": f"No pages found to delete for path: {root_path}",
                "delete_mode": delete_mode,
                "root_found": root_page is not None,
                "children_found": len(child_pages)
            })
        
        # Sort by depth (deepest first) to avoid dependency issues
        pages_to_delete.sort(key=lambda x: x["path"].count("/"), reverse=True)
        
        # Delete pages
        deleted_pages = []
        failed_deletions = []
        
        for page in pages_to_delete:
            try:
                delete_result = await wikijs_delete_page(
                    page_id=page["id"], 
                    remove_file_mapping=remove_file_mappings
                )
                delete_data = json.loads(delete_result)
                
                if "error" not in delete_data:
                    deleted_pages.append({
                        "pageId": page["id"],
                        "title": page["title"],
                        "path": page["path"],
                        "depth": page["path"].count("/")
                    })
                else:
                    failed_deletions.append({
                        "pageId": page["id"],
                        "title": page["title"],
                        "path": page["path"],
                        "error": delete_data["error"]
                    })
            except Exception as e:
                failed_deletions.append({
                    "pageId": page["id"],
                    "title": page["title"],
                    "path": page["path"],
                    "error": str(e)
                })
        
        result = {
            "root_path": root_path,
            "delete_mode": delete_mode,
            "total_found": len(pages_to_delete),
            "deleted_count": len(deleted_pages),
            "failed_count": len(failed_deletions),
            "deleted_pages": deleted_pages,
            "failed_deletions": failed_deletions,
            "hierarchy_summary": {
                "root_page_found": root_page is not None,
                "child_pages_found": len(child_pages),
                "max_depth": max([p["path"].count("/") for p in pages_to_delete]) if pages_to_delete else 0
            },
            "status": "completed"
        }
        
        logger.info(f"Hierarchy deletion completed for {root_path}: {len(deleted_pages)} deleted, {len(failed_deletions)} failed")
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Hierarchy deletion failed: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

@mcp.tool()
async def wikijs_cleanup_orphaned_mappings() -> str:
    """
    Clean up file-to-page mappings for pages that no longer exist in Wiki.js.
    
    Returns:
        JSON string with cleanup results
    """
    try:
        await wikijs.authenticate()
        db = get_db()
        
        # Get all file mappings
        mappings = db.query(FileMapping).all()
        
        if not mappings:
            return json.dumps({
                "message": "No file mappings found",
                "cleaned_count": 0
            })
        
        # Check which pages still exist
        orphaned_mappings = []
        valid_mappings = []
        
        for mapping in mappings:
            try:
                get_query = """
                query($id: Int!) {
                    pages {
                        single(id: $id) {
                            id
                            title
                            path
                        }
                    }
                }
                """
                get_response = await wikijs.graphql_request(get_query, {"id": mapping.page_id})
                page_data = get_response.get("data", {}).get("pages", {}).get("single")
                
                if page_data:
                    valid_mappings.append({
                        "file_path": mapping.file_path,
                        "page_id": mapping.page_id,
                        "page_title": page_data["title"]
                    })
                else:
                    orphaned_mappings.append({
                        "file_path": mapping.file_path,
                        "page_id": mapping.page_id,
                        "last_updated": mapping.last_updated.isoformat() if mapping.last_updated else None
                    })
                    # Delete orphaned mapping
                    db.delete(mapping)
                    
            except Exception as e:
                # If we can't check the page, consider it orphaned
                orphaned_mappings.append({
                    "file_path": mapping.file_path,
                    "page_id": mapping.page_id,
                    "error": str(e)
                })
                db.delete(mapping)
        
        db.commit()
        
        result = {
            "total_mappings": len(mappings),
            "valid_mappings": len(valid_mappings),
            "orphaned_mappings": len(orphaned_mappings),
            "cleaned_count": len(orphaned_mappings),
            "orphaned_details": orphaned_mappings,
            "status": "completed"
        }
        
        logger.info(f"Cleaned up {len(orphaned_mappings)} orphaned file mappings")
        return json.dumps(result)
        
    except Exception as e:
        error_msg = f"Cleanup failed: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

def main():
    """Main entry point for the MCP server."""
    import asyncio
    
    async def run_server():
        await wikijs.authenticate()
        logger.info("Wiki.js MCP Server started")
        
    # Run the server
    mcp.run()

if __name__ == "__main__":
    main() 