import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { CoverLetter, CvInfo } from '../api/types'

export function useCv() {
  return useQuery({
    queryKey: ['cv'],
    queryFn: () => api.get<CvInfo>('/cv'),
  })
}

export function useCoverLetters() {
  return useQuery({
    queryKey: ['cover-letters'],
    queryFn: () => api.get<{ items: CoverLetter[] }>('/cover-letters'),
    select: (data) => data.items,
  })
}

function useInvalidateCv() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: ['cv'] })
    void queryClient.invalidateQueries({ queryKey: ['me'] })
    void queryClient.invalidateQueries({ queryKey: ['jobs'] })
  }
}

export function useUploadCv() {
  const invalidate = useInvalidateCv()
  return useMutation({
    // `consent` is the checkbox; the API refuses (consent_required) an
    // upload without it unless consent is already on record.
    mutationFn: ({ file, consent }: { file: File; consent: boolean }) =>
      api.putFile<CvInfo>('/cv', file, { consent: String(consent) }),
    onSuccess: invalidate,
  })
}

export function useDeleteCv() {
  const invalidate = useInvalidateCv()
  return useMutation({
    mutationFn: () => api.delete('/cv'),
    onSuccess: invalidate,
  })
}
