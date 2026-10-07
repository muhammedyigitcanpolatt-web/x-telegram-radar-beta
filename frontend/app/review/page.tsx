"use client";
import Link from "next/link";
import { AuthGate } from "@/components/AuthGate";
import { LexiconReviewPanel } from "@/components/dashboard/LexiconReviewPanel";
import { AlertReviewPanel } from "@/components/dashboard/AlertReviewPanel";
export default function ReviewPage() {
  return <AuthGate><main className="min-h-screen p-6 space-y-6"><Link href="/" className="text-accent">← Back to command center</Link><LexiconReviewPanel /><AlertReviewPanel /></main></AuthGate>;
}
