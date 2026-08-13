"""
API 请求/响应模型
"""
from typing import Optional
from pydantic import BaseModel, Field
from muye_multi_agent_sdk import ChannelInvokeResponse


class UserLocation(BaseModel):
    """用户地理位置"""
    lat: float = Field(..., description="纬度，示例：34.26")
    lng: float = Field(..., description="经度，示例：108.94")


class UserInformation(BaseModel):
    """用户个人信息设置"""
    name: str = Field(default=None, description="用户给AI助手起的名字")


class ChatRequest(BaseModel):
    """对话请求"""
    user_id: str = Field(default="default_user", description="用户ID")
    session_id: str = Field(default="default_session", description="会话ID")
    user_input: str = Field(..., description="用户消息")
    stream: Optional[bool] = Field(default=True, description="是否流式输出")
    files: Optional[list] = Field(default=None, description="上传的文件信息列表")
    user_location: Optional[UserLocation] = Field(default=None, description="用户地理位置")
    enable_knowledge: bool = Field(default=False, description="是否启用知识检索")
    user_informations:Optional[UserInformation] = Field(default=None, description="用户自定义的身份信息")


class ChatResponse(BaseModel):
    """对话响应"""
    success: bool
    user_id: str
    session_id: str
    message: str
    error: Optional[str] = None


ChannelResponse = ChannelInvokeResponse
