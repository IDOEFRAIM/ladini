"""Compatibility exports from service-first scraper modules."""

from agriconnect.services.scraper.scrapers.institutional_pdf_harvester import InstitutionalPdfHarvester

AnamBulletinScraper = InstitutionalPdfHarvester
FewsNetScraper = InstitutionalPdfHarvester
SonagessScraper = InstitutionalPdfHarvester

FewsPdfHarvester = AnamBulletinScraper

__all__ = [
	"AnamBulletinScraper",
	"FewsPdfHarvester",
	"FewsNetScraper",
	"SonagessScraper",
	"InstitutionalPdfHarvester",
]
