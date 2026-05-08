"""Cross-cutting insights modules — Agent Doctor + Topic extractor."""
from .agent_doctor import (  # noqa: F401
    AgentDoctorReport,
    Prescription,
    SpecificChange,
    generate,
    generate_async,
    render_markdown,
    write_doctor_artifacts,
)
from .topic_extractor import (  # noqa: F401
    Topic,
    TopicExtractionResult,
    extract_topics,
    extract_topics_async,
)
