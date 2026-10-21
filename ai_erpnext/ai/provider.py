import abc
import json
import typing


class LLMProvider(abc.ABC):
	@abc.abstractmethod
	def extract_document_data(
		self, file_content: bytes, file_type: str, prompt: str
	) -> dict[str, typing.Any]:
		"""Extract structured JSON data from a document file."""
		pass
